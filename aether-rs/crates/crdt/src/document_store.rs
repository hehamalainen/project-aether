use crate::rga::RGADocument;
use diffy::Patch;
use models::entities::Proposal;
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use regex::Regex;

const RESERVED_PATH_SEGMENTS: &[&str] = &[".git"];
const RESERVED_FILENAMES: &[&str] = &[
    ".aether_snapshot_manifest.json",
    ".aether_crdt_state.json",
];

fn is_reserved_segment(part: &str) -> bool {
    let lowered = part.to_lowercase();
    let stripped_right = lowered.trim_end_matches(&[' ', '.'][..]);
    let stripped_spaces = lowered.trim().trim_end_matches('.');
    RESERVED_PATH_SEGMENTS.contains(&lowered.as_str())
        || RESERVED_PATH_SEGMENTS.contains(&stripped_right)
        || RESERVED_PATH_SEGMENTS.contains(&stripped_spaces)
        || stripped_right.starts_with("git~")
        || stripped_right.starts_with(".git~")
}

fn is_reserved_filename(name: &str) -> bool {
    let lowered = name.to_lowercase();
    let stripped_right = lowered.trim_end_matches(&[' ', '.'][..]);
    let stripped_spaces = lowered.trim().trim_end_matches('.');
    RESERVED_FILENAMES.contains(&lowered.as_str())
        || RESERVED_FILENAMES.contains(&stripped_right)
        || RESERVED_FILENAMES.contains(&stripped_spaces)
}

pub fn validate_repo_relative_path(path: &str) -> Result<String, String> {
    let cleaned = path.trim();
    if cleaned.is_empty() {
        return Err("Path must not be empty".to_string());
    }
    if cleaned.contains('\0') || cleaned.contains('\r') || cleaned.contains('\n') {
        return Err(format!("Path contains forbidden control characters: {:?}", path));
    }
    if cleaned.contains('\\') {
        return Err(format!("Path must use forward slashes only: {:?}", path));
    }
    if cleaned.starts_with('/') || cleaned.starts_with('~') || cleaned.ends_with('/') {
        return Err(format!("Path must be a relative repository path: {:?}", path));
    }

    let parts: Vec<&str> = cleaned.split('/').collect();
    if parts.iter().any(|p| p.is_empty()) {
        return Err(format!("Path contains empty segment: {:?}", path));
    }
    if parts[0].contains(':') {
        return Err(format!("Path must not include a drive or scheme prefix: {:?}", path));
    }

    for part in &parts {
        if *part == "." || *part == ".." {
            return Err(format!("Path traversal segment is not allowed: {:?}", path));
        }
        if is_reserved_segment(part) {
            return Err(format!("Reserved path segment '{}' is not allowed: {:?}", part, path));
        }
    }

    if let Some(last) = parts.last() {
        if is_reserved_filename(last) {
            return Err(format!("Reserved filename '{}' is not allowed: {:?}", last, path));
        }
    }

    Ok(parts.join("/"))
}

fn resolve_base_directory(base_dir: &Path) -> Result<PathBuf, String> {
    if let Ok(meta) = std::fs::symlink_metadata(base_dir) {
        if meta.file_type().is_symlink() {
            return Err(format!("Base directory must not be a symlink: {:?}", base_dir));
        }
    }
    if base_dir.exists() {
        return std::fs::canonicalize(base_dir)
            .map_err(|e| format!("Failed to canonicalize base directory {:?}: {}", base_dir, e));
    }

    let mut existing = base_dir.to_path_buf();
    let mut missing_parts = Vec::new();
    while !existing.exists() {
        if let Some(name) = existing.file_name() {
            missing_parts.push(name.to_os_string());
        } else {
            break;
        }
        if !existing.pop() {
            break;
        }
    }

    let mut resolved = if existing.as_os_str().is_empty() {
        std::env::current_dir().map_err(|e| e.to_string())?
    } else {
        if let Ok(meta) = std::fs::symlink_metadata(&existing) {
            if meta.file_type().is_symlink() {
                return Err(format!("Base directory ancestor must not be a symlink: {:?}", existing));
            }
        }
        std::fs::canonicalize(&existing).map_err(|e| e.to_string())?
    };

    for part in missing_parts.into_iter().rev() {
        resolved.push(part);
    }
    Ok(resolved)
}

pub fn resolve_contained_path(base_dir: &Path, rel_path: &str) -> Result<PathBuf, String> {
    let normalized = validate_repo_relative_path(rel_path)?;
    let base_resolved = resolve_base_directory(base_dir)?;

    let mut current = base_resolved.clone();
    for part in normalized.split('/') {
        current.push(part);
        if let Ok(meta) = std::fs::symlink_metadata(&current) {
            if meta.file_type().is_symlink() {
                return Err(format!("Symlink escape detected in path: {:?}", rel_path));
            }
            let resolved_step = std::fs::canonicalize(&current)
                .map_err(|e| format!("Failed to resolve path {:?}: {}", current, e))?;
            if resolved_step == base_resolved || !resolved_step.starts_with(&base_resolved) {
                return Err(format!("Resolved path escapes base directory: {:?}", rel_path));
            }
            if let Ok(rel) = resolved_step.strip_prefix(&base_resolved) {
                for comp in rel.components() {
                    if let Some(comp_str) = comp.as_os_str().to_str() {
                        if is_reserved_segment(comp_str) {
                            return Err(format!(
                                "Reserved path segment '{}' is not allowed: {:?}",
                                comp_str, rel_path
                            ));
                        }
                    }
                }
            }
        }
    }

    if let Some(name) = current.file_name().and_then(|n| n.to_str()) {
        if is_reserved_filename(name) {
            return Err(format!("Reserved filename '{}' is not allowed: {:?}", name, rel_path));
        }
    }

    Ok(current)
}

pub fn validate_proposal_paths(
    change_type: &str,
    change_content: &str,
    affected_paths: &[String],
) -> Result<Vec<String>, String> {
    let mut normalized_paths = Vec::new();
    for raw_path in affected_paths {
        let norm = validate_repo_relative_path(raw_path)?;
        if !normalized_paths.contains(&norm) {
            normalized_paths.push(norm);
        }
    }

    match change_type {
        "full_file" => {
            if normalized_paths.is_empty() {
                return Err("full_file proposal requires at least one valid affected_path".to_string());
            }
            Ok(normalized_paths)
        }
        "unified_diff" => {
            let parsed = parse_unified_diff(change_content)?;
            if parsed.is_empty() {
                return Err("unified_diff proposal did not contain any valid file hunks".to_string());
            }
            let mut diff_paths: Vec<String> = parsed.keys().cloned().collect();
            diff_paths.sort();
            for diff_path in diff_paths {
                let norm = validate_repo_relative_path(&diff_path)?;
                if !normalized_paths.contains(&norm) {
                    normalized_paths.push(norm);
                }
            }
            if normalized_paths.is_empty() {
                return Err("unified_diff proposal requires at least one valid path".to_string());
            }
            Ok(normalized_paths)
        }
        other => Err(format!("Unsupported change_type: {}", other)),
    }
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct FileDocument {
    pub path: String,
    pub document: RGADocument,
    pub proposal_history: Vec<String>,
    pub last_hash: Option<String>,
}

#[derive(Clone)]
pub struct DocumentStore {
    pub repo_id: String,
    pub documents: HashMap<String, FileDocument>,
    shared_paths: HashSet<String>,
    replica_counter: usize,
    pub crdt_mode: Option<String>,
}

impl DocumentStore {
    pub fn new(repo_id: String) -> Self {
        Self {
            repo_id,
            documents: HashMap::new(),
            shared_paths: HashSet::new(),
            replica_counter: 0,
            crdt_mode: None,
        }
    }

    fn get_replica_id(&mut self) -> String {
        self.replica_counter += 1;
        format!("{}_{}", self.repo_id, self.replica_counter)
    }

    /// Implement explicit copy-on-write clone semantics
    pub fn cow_clone(&mut self) -> Self {
        let mut cloned = Self {
            repo_id: self.repo_id.clone(),
            documents: self.documents.clone(),
            shared_paths: HashSet::new(),
            replica_counter: self.replica_counter,
            crdt_mode: self.crdt_mode.clone(),
        };

        // All paths are now shared between self and clone
        for path in self.documents.keys() {
            self.shared_paths.insert(path.clone());
            cloned.shared_paths.insert(path.clone());
        }

        cloned
    }

    fn ensure_writable(&mut self, path: &str) -> &mut FileDocument {
        if self.shared_paths.contains(path) {
            if let Some(doc) = self.documents.get(path) {
                let mut new_doc = doc.clone();
                // Deep clone the RGA document to ensure isolation
                new_doc.document = doc.document.clone();
                self.documents.insert(path.to_string(), new_doc);
            }
            self.shared_paths.remove(path);
        }
        self.documents.get_mut(path).unwrap()
    }

    fn get_or_create_document(&mut self, path: &str) -> &mut FileDocument {
        if !self.documents.contains_key(path) {
            let replica_id = self.get_replica_id();
            let doc = FileDocument {
                path: path.to_string(),
                document: RGADocument::new(replica_id),
                proposal_history: Vec::new(),
                last_hash: None,
            };
            self.documents.insert(path.to_string(), doc);
        }
        self.ensure_writable(path)
    }

    pub fn apply_token_smart_edit_bounded(
        &mut self,
        file_path: &str,
        new_content: &str,
        _proposal_id: &str,
        active_dot: Option<&crate::causality::Dot>,
    ) {
        let file_doc = self.get_or_create_document(file_path);
        let old_text = file_doc.document.get_text();

        // 1. Calculate common prefix
        let old_chars: Vec<char> = old_text.chars().collect();
        let new_chars: Vec<char> = new_content.chars().collect();

        let mut prefix_len = 0;
        while prefix_len < old_chars.len() && prefix_len < new_chars.len() && old_chars[prefix_len] == new_chars[prefix_len] {
            prefix_len += 1;
        }

        // 2. Calculate common suffix
        let mut suffix_len = 0;
        while suffix_len < (old_chars.len() - prefix_len) && suffix_len < (new_chars.len() - prefix_len) {
            let old_idx = old_chars.len() - 1 - suffix_len;
            let new_idx = new_chars.len() - 1 - suffix_len;
            if old_chars[old_idx] == new_chars[new_idx] {
                suffix_len += 1;
            } else {
                break;
            }
        }

        let mut edit_applied = false;

        // Check if it's a single insertion
        if prefix_len + suffix_len == old_chars.len() && new_chars.len() > old_chars.len() {
            let insert_str: String = new_chars[prefix_len .. new_chars.len() - suffix_len].iter().collect();
            file_doc.document.local_char_insert_split(prefix_len, insert_str);
            edit_applied = true;
        }
        // Check if it's a single deletion
        else if prefix_len + suffix_len == new_chars.len() && old_chars.len() > new_chars.len() {
            let delete_len = old_chars.len() - prefix_len - suffix_len;
            file_doc.document.local_char_delete_split_bounded(prefix_len, delete_len, active_dot);
            edit_applied = true;
        }

        // Fallback to full replace_tokens if not a simple single insert/delete
        if !edit_applied {
            let tokens = crate::structured_doc::tokenize_source(new_content);
            file_doc.document.replace_tokens_bounded(&tokens, active_dot);
        }
    }

    fn apply_token_smart_edit(&mut self, file_path: &str, new_content: &str, proposal_id: &str) {
        self.apply_token_smart_edit_bounded(file_path, new_content, proposal_id, None);
    }

    pub fn apply_full_file_bounded(
        &mut self,
        file_path: &str,
        content: &str,
        proposal_id: &str,
        active_dot: Option<&crate::causality::Dot>,
    ) {
        let file_doc = self.get_or_create_document(file_path);

        let old_text = file_doc.document.get_text();
        if old_text == content {
            return;
        }

        let env_mode = std::env::var("AETHER_CRDT_MODE").ok();
        let mode = self.crdt_mode.as_deref()
            .or(env_mode.as_deref())
            .unwrap_or_default();
        if mode == "token" {
            self.apply_token_smart_edit_bounded(file_path, content, proposal_id, active_dot);
        } else {
            let file_doc = self.ensure_writable(file_path);
            file_doc.document.replace_text(content);
        }
        let file_doc = self.ensure_writable(file_path);
        file_doc.proposal_history.push(proposal_id.to_string());
        file_doc.last_hash = None;
    }

    pub fn apply_full_file(&mut self, file_path: &str, content: &str, proposal_id: &str) {
        self.apply_full_file_bounded(file_path, content, proposal_id, None);
    }


    pub fn apply_diff(
        &mut self,
        diff_text: &str,
        proposal_id: &str,
    ) -> Result<Vec<String>, String> {
        let _ = parse_unified_diff(diff_text)?;
        let mut affected_paths = Vec::new();

        // Simple heuristic to split multi-file diffs:
        let chunks: Vec<&str> = diff_text.split("\ndiff --git ").collect();
        for (i, chunk) in chunks.iter().enumerate() {
            let mut diff_str = chunk.to_string();
            if i > 0 && !diff_str.starts_with("diff --git ") {
                diff_str = format!("diff --git {}", diff_str);
            } else if i == 0
                && !diff_str.starts_with("diff --git ")
                && !diff_str.starts_with("--- ")
            {
                // Not a valid diff start, skip
                continue;
            }

            let patch = match Patch::from_str(&diff_str) {
                Ok(p) => p,
                Err(_e) => {
                    // Try parsing without diff --git prefix if it failed
                    if let Ok(p) = Patch::from_str(chunk) {
                        p
                    } else {
                        continue;
                    }
                }
            };

            let mut file_path = String::new();
            for line in diff_str.lines() {
                if let Some(stripped) = line.strip_prefix("+++ ") {
                    let candidate = stripped.split('\t').next().unwrap_or("").trim();
                    if candidate != "/dev/null" {
                        let rel = candidate.strip_prefix("b/").unwrap_or(candidate);
                        file_path = validate_repo_relative_path(rel)?;
                        break;
                    }
                } else if let Some(stripped) = line.strip_prefix("--- ") {
                    let candidate = stripped.split('\t').next().unwrap_or("").trim();
                    if candidate != "/dev/null" && file_path.is_empty() {
                        let rel = candidate.strip_prefix("a/").unwrap_or(candidate);
                        file_path = validate_repo_relative_path(rel)?;
                    }
                }
            }

            if file_path.is_empty() || file_path == "/dev/null" {
                continue;
            }
            file_path = validate_repo_relative_path(&file_path)?;

            let old_text = {
                let file_doc = self.get_or_create_document(&file_path);
                file_doc.document.get_text()
            };

            match diffy::apply(&old_text, &patch) {
                Ok(new_text) => {
                    if old_text != new_text {
                        let env_mode = std::env::var("AETHER_CRDT_MODE").ok();
                        let mode = self.crdt_mode.as_deref()
                            .or(env_mode.as_deref())
                            .unwrap_or_default();
                        if mode == "token" {
                            self.apply_token_smart_edit(&file_path, &new_text, proposal_id);
                        } else {
                            let file_doc = self.ensure_writable(&file_path);
                            file_doc.document.replace_text(&new_text);
                        }

                        let file_doc = self.ensure_writable(&file_path);
                        file_doc.proposal_history.push(proposal_id.to_string());
                        file_doc.last_hash = None;
                        affected_paths.push(file_path);
                    }
                }

                Err(e) => return Err(format!("Failed to apply patch to {}: {}", file_path, e)),
            }
        }

        Ok(affected_paths)
    }

    pub fn get_state_hash(&mut self) -> String {
        let mut paths: Vec<String> = self.documents.keys().cloned().collect();
        paths.sort();

        let mut hasher = Sha256::new();
        for path in paths {
            let doc = self.documents.get(&path).unwrap();
            let file_hash = if let Some(h) = &doc.last_hash {
                h.clone()
            } else {
                let content = doc.document.get_text();
                let mut file_hasher = Sha256::new();
                file_hasher.update(content.as_bytes());
                let file_hash = format!("{:x}", file_hasher.finalize());
                
                if !self.shared_paths.contains(&path) {
                    if let Some(doc_mut) = self.documents.get_mut(&path) {
                        doc_mut.last_hash = Some(file_hash.clone());
                    }
                }
                file_hash
            };

            hasher.update(path.as_bytes());
            hasher.update(b"\0");
            hasher.update(file_hash.as_bytes());
        }

        format!("{:x}", hasher.finalize())
    }

    pub fn find_overlapping_proposals(&self, proposals: &[Proposal]) -> Vec<Vec<String>> {
        // Geometric conflict detection is deferred.
        // Match existing Python behavior: coarse file-level overlap detection.
        let mut path_to_props: HashMap<String, Vec<String>> = HashMap::new();

        for p in proposals {
            let paths: Vec<String> = serde_json::from_str(&p.affected_paths).unwrap_or_default();
            for path in paths {
                path_to_props.entry(path).or_default().push(p.id.clone());
            }
        }

        let mut overlaps = Vec::new();
        let mut seen = HashSet::new();

        for props in path_to_props.values() {
            if props.len() > 1 {
                let mut sorted_props = props.clone();
                sorted_props.sort();
                let key = sorted_props.join(",");
                if !seen.contains(&key) {
                    seen.insert(key);
                    overlaps.push(sorted_props);
                }
            }
        }

        overlaps
    }

    pub fn load_from_snapshot(&mut self, snapshot_path: &Path) -> Result<(), String> {
        if let Ok(meta) = std::fs::symlink_metadata(snapshot_path) {
            if meta.file_type().is_symlink() {
                return Err(format!("Snapshot path must not be a symlink: {:?}", snapshot_path));
            }
        }
        if !snapshot_path.exists() || !snapshot_path.is_dir() {
            return Err(format!("Snapshot path does not exist: {:?}", snapshot_path));
        }

        let crdt_state_path = snapshot_path.join(".aether_crdt_state.json");
        if crdt_state_path.exists() {
            if let Ok(meta) = std::fs::symlink_metadata(&crdt_state_path) {
                if meta.file_type().is_symlink() {
                    return Err("CRDT state file must not be a symlink".to_string());
                }
            }
            if let Ok(state_json) = std::fs::read_to_string(&crdt_state_path) {
                if let Ok(docs) = serde_json::from_str::<HashMap<String, FileDocument>>(&state_json) {
                    let mut validated_docs = HashMap::new();
                    for (raw_key, mut doc) in docs {
                        let norm_key = validate_repo_relative_path(&raw_key)?;
                        doc.path = norm_key.clone();
                        validated_docs.insert(norm_key, doc);
                    }
                    self.documents = validated_docs;
                    return Ok(());
                }
            }
        }

        let manifest_path = snapshot_path.join(".aether_snapshot_manifest.json");
        let mode = if manifest_path.exists() {
            let manifest_str = std::fs::read_to_string(&manifest_path)
                .map_err(|e| format!("Failed to read manifest: {}", e))?;
            let manifest: serde_json::Value = serde_json::from_str(&manifest_str)
                .map_err(|e| format!("Malformed manifest JSON: {}", e))?;

            if manifest.get("mode").and_then(|m| m.as_str()) == Some("delta") {
                let base_id = manifest
                    .get("base_snapshot_id")
                    .and_then(|id| id.as_str())
                    .ok_or("Delta manifest missing base_snapshot_id")?;
                let norm_base_id = validate_repo_relative_path(base_id)?;
                if norm_base_id.contains('/') {
                    return Err(format!("Invalid base_snapshot_id: {}", base_id));
                }

                let parent_dir = snapshot_path
                    .parent()
                    .ok_or("Snapshot path has no parent directory")?;
                let base_path = resolve_contained_path(parent_dir, &norm_base_id)?;

                // Recursively load base snapshot
                self.load_from_snapshot(&base_path)?;

                // Process deleted paths
                if let Some(deleted) = manifest.get("deleted_paths").and_then(|d| d.as_array()) {
                    for path_val in deleted {
                        if let Some(path_str) = path_val.as_str() {
                            let norm_del = validate_repo_relative_path(path_str)?;
                            self.documents.remove(&norm_del);
                        }
                    }
                }

                // Strictly load changed_paths
                let changed = manifest
                    .get("changed_paths")
                    .and_then(|c| c.as_array())
                    .ok_or("Delta manifest missing or malformed changed_paths")?;

                for path_val in changed {
                    let path_str = path_val.as_str().ok_or("Invalid changed_path element")?;
                    let norm_path = validate_repo_relative_path(path_str)?;
                    let full_path = resolve_contained_path(snapshot_path, &norm_path)?;
                    if !full_path.exists() {
                        return Err(format!(
                            "File in changed_paths is missing on disk: {}",
                            path_str
                        ));
                    }
                    let content = std::fs::read_to_string(&full_path)
                        .map_err(|e| format!("Failed to read changed file {}: {}", path_str, e))?;
                    self.apply_full_file(&norm_path, &content, "snapshot_load");
                }

                Some("delta".to_string())
            } else {
                Some("full".to_string())
            }
        } else {
            None
        };

        if mode != Some("delta".to_string()) {
            let walker = walkdir::WalkDir::new(snapshot_path);
            for entry_res in walker.into_iter() {
                let entry = match entry_res {
                    Ok(e) => e,
                    Err(err) => {
                        eprintln!("WALKDIR ERROR: {:?}", err);
                        continue;
                    }
                };
                if entry.path_is_symlink() {
                    return Err(format!(
                        "Symlink detected inside snapshot directory: {:?}",
                        entry.path()
                    ));
                }
                if entry.file_type().is_file() {
                    let path = entry.path();
                    if let Some(fname) = path.file_name().and_then(|n| n.to_str()) {
                        if is_reserved_filename(fname) {
                            continue;
                        }
                    }

                    if let Ok(rel_path) = path.strip_prefix(snapshot_path) {
                        if let Some(rel_str) = rel_path.to_str() {
                            let norm_rel = validate_repo_relative_path(rel_str)?;
                            resolve_contained_path(snapshot_path, &norm_rel)?;
                            let content = std::fs::read_to_string(path).unwrap_or_default();
                            self.apply_full_file(&norm_rel, &content, "snapshot_load");
                        }
                    }
                }
            }
        }

        Ok(())
    }

    pub fn materialize_snapshot(&self, output_path: &Path) -> Result<(usize, usize), String> {
        if let Ok(meta) = std::fs::symlink_metadata(output_path) {
            if meta.file_type().is_symlink() {
                return Err(format!(
                    "Snapshot output directory must not be a symlink: {:?}",
                    output_path
                ));
            }
        }
        std::fs::create_dir_all(output_path).map_err(|e| e.to_string())?;

        // Pre-validate all document paths before writing any file to disk
        let mut validated_targets = Vec::with_capacity(self.documents.len());
        for (path, file_doc) in &self.documents {
            let norm_path = validate_repo_relative_path(path)?;
            let target = resolve_contained_path(output_path, &norm_path)?;
            validated_targets.push((norm_path, target, file_doc));
        }

        let mut file_count = 0;
        let mut total_bytes = 0;

        for (norm_path, full_path, file_doc) in validated_targets {
            if let Some(parent) = full_path.parent() {
                std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
            }
            let verified_path = resolve_contained_path(output_path, &norm_path)?;
            let content = file_doc.document.get_text();
            std::fs::write(&verified_path, &content).map_err(|e| e.to_string())?;

            file_count += 1;
            total_bytes += content.len();
        }

        let state_path = output_path.join(".aether_crdt_state.json");
        if let Ok(meta) = std::fs::symlink_metadata(&state_path) {
            if meta.file_type().is_symlink() {
                return Err("Refusing to write .aether_crdt_state.json through symlink".to_string());
            }
        }
        if let Ok(state_json) = serde_json::to_string(&self.documents) {
            let _ = std::fs::write(&state_path, state_json);
        }

        Ok((file_count, total_bytes))
    }

    pub fn merge(&mut self, other: &DocumentStore) -> usize {
        let mut applied = 0;
        for (path, other_doc) in &other.documents {
            if let Some(my_doc) = self.documents.get_mut(path) {
                applied += my_doc.document.merge(&other_doc.document);
                my_doc.proposal_history.extend(other_doc.proposal_history.clone());
                my_doc.proposal_history.sort();
                my_doc.proposal_history.dedup();
                my_doc.last_hash = None;
            } else {
                self.documents.insert(path.clone(), other_doc.clone());
                applied += 1;
            }
        }
        applied
    }

    pub fn apply_diff_batch(
        &mut self,
        batched_diffs: &[(&str, &str)], // &[(diff_text, proposal_id)]
    ) -> Result<Vec<String>, String> {
        let mut hunks_by_file: HashMap<String, Vec<(DiffHunk, String)>> = HashMap::new();

        for &(diff_text, proposal_id) in batched_diffs {
            let parsed = parse_unified_diff(diff_text)?;
            for (path, hunks) in parsed {
                for hunk in hunks {
                    hunks_by_file.entry(path.clone()).or_default().push((hunk, proposal_id.to_string()));
                }
            }
        }

        // Verify disjointness per file
        for (path, hunk_list) in &mut hunks_by_file {
            // Sort by old_start to check overlap easily
            hunk_list.sort_by_key(|x| x.0.old_start);
            for idx in 0..hunk_list.len().saturating_sub(1) {
                let (h1, prop1) = &hunk_list[idx];
                let (h2, prop2) = &hunk_list[idx + 1];
                let h1_end = h1.old_start + h1.old_count.max(1) - 1;
                let h2_start = h2.old_start;
                if h1_end >= h2_start {
                    return Err(format!(
                        "Spatial overlap detected between proposal {} and {} on file {}",
                        prop1, prop2, path
                    ));
                }
            }
        }

        let mut affected_paths = Vec::new();

        for (path, mut hunk_list) in hunks_by_file {
            let current_text = {
                let file_doc = self.get_or_create_document(&path);
                file_doc.document.get_text()
            };

            let mut lines: Vec<String> = if current_text.is_empty() {
                Vec::new()
            } else {
                current_text.split('\n').map(|s| s.to_string()).collect()
            };

            // Apply hunks in descending order of old_start
            hunk_list.sort_by_key(|x| std::cmp::Reverse(x.0.old_start));
            for (hunk, _) in &hunk_list {
                lines = apply_hunk_to_lines(&lines, hunk);
                
                // Edge case: if we ended up with empty lines, check if it was a pure insertion into empty file
                if lines.is_empty()
                    && hunk.lines.iter().all(|l| l.starts_with('+') || l.is_empty())
                    && hunk.old_start == 0
                    && hunk.old_count == 0
                {
                    lines = hunk.lines.iter()
                        .filter(|l| l.starts_with('+'))
                        .map(|l| l[1..].to_string())
                        .collect();
                }
            }

            let new_text = lines.join("\n");

            if current_text != new_text {
                let env_mode = std::env::var("AETHER_CRDT_MODE").ok();
                let mode = self.crdt_mode.as_deref()
                    .or(env_mode.as_deref())
                    .unwrap_or_default();
                if mode == "token" {
                    self.apply_token_smart_edit(&path, &new_text, "");
                } else {
                    let file_doc = self.ensure_writable(&path);
                    file_doc.document.replace_text(&new_text);
                }
            }

            // Update proposal history
            let file_doc = self.ensure_writable(&path);
            file_doc.last_hash = None;
            for (_, prop_id) in hunk_list {
                if !file_doc.proposal_history.contains(&prop_id) {
                    file_doc.proposal_history.push(prop_id);
                }
            }

            affected_paths.push(path);
        }

        affected_paths.sort();
        Ok(affected_paths)
    }
}

#[derive(Debug, Clone)]
pub struct DiffHunk {
    pub old_start: usize,
    pub old_count: usize,
    pub new_start: usize,
    pub new_count: usize,
    pub lines: Vec<String>,
}

pub fn parse_unified_diff(diff_text: &str) -> Result<HashMap<String, Vec<DiffHunk>>, String> {
    let mut files = HashMap::new();
    let mut current_file: Option<String> = None;
    let mut current_git_file: Option<String> = None;
    let mut current_hunks = Vec::new();

    let clean_diff = diff_text.trim_end_matches('\r').trim_end_matches('\n');
    let lines: Vec<&str> = clean_diff.lines().collect();
    
    let git_header_re = Regex::new(r"^diff --git a/(.+) b/(.+)$").unwrap();
    let hunk_header_re = Regex::new(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@").unwrap();

    let mut i = 0;
    while i < lines.len() {
        let line = lines[i];

        if line.starts_with("diff --git") {
            if let Some(ref file_path) = current_file {
                if !current_hunks.is_empty() {
                    files.insert(file_path.clone(), current_hunks);
                }
            }
            current_hunks = Vec::new();
            if let Some(caps) = git_header_re.captures(line) {
                let norm_a = validate_repo_relative_path(&caps[1])?;
                let norm_b = validate_repo_relative_path(&caps[2])?;
                if norm_a != norm_b {
                    return Err(format!(
                        "Mismatched diff --git paths: '{}' vs '{}'",
                        norm_a, norm_b
                    ));
                }
                current_git_file = Some(norm_b.clone());
                current_file = Some(norm_b);
            } else {
                return Err(format!("Malformed diff --git header: {}", line));
            }
            i += 1;
            continue;
        }

        if line.starts_with("---") || line.starts_with("+++") {
            let is_minus = line.starts_with("---");
            let raw_rest = &line[3..];
            if !raw_rest.is_empty() && !raw_rest.starts_with(' ') && !raw_rest.starts_with('\t') {
                return Err(format!("Malformed file header: {}", line));
            }
            let candidate = raw_rest.trim_start().split('\t').next().unwrap_or("").trim();
            if candidate.is_empty() {
                return Err(format!("Empty file header in diff: {}", line));
            }
            if candidate != "/dev/null" {
                let stripped = if is_minus {
                    candidate.strip_prefix("a/").unwrap_or(candidate)
                } else {
                    candidate.strip_prefix("b/").unwrap_or(candidate)
                };
                let norm_header = validate_repo_relative_path(stripped)?;
                if let Some(ref git_file) = current_git_file {
                    if &norm_header != git_file {
                        return Err(format!(
                            "Diff header path '{}' does not match diff --git path '{}'",
                            norm_header, git_file
                        ));
                    }
                }
                if let Some(ref existing_file) = current_file {
                    if &norm_header != existing_file && !current_hunks.is_empty() {
                        files.insert(existing_file.clone(), current_hunks);
                        current_hunks = Vec::new();
                    }
                }
                current_file = Some(norm_header);
            }
            i += 1;
            continue;
        }

        if line.starts_with("@@") {
            if current_file.is_none() {
                return Err("Diff hunk encountered without a valid file header".to_string());
            }
            if let Some(caps) = hunk_header_re.captures(line) {
                let old_start = caps[1].parse::<usize>().map_err(|e| format!("Invalid old_start: {}", e))?;
                let old_count = caps.get(2)
                    .and_then(|m| m.as_str().parse::<usize>().ok())
                    .unwrap_or(1);
                
                let new_start = caps[3].parse::<usize>().map_err(|e| format!("Invalid new_start: {}", e))?;
                let new_count = caps.get(4)
                    .and_then(|m| m.as_str().parse::<usize>().ok())
                    .unwrap_or(1);

                let mut hunk_lines = Vec::new();
                i += 1;
                while i < lines.len() {
                    let hunk_line = lines[i];
                    if hunk_line.starts_with("diff --git")
                        || hunk_line.starts_with("@@")
                        || hunk_line.starts_with("---")
                        || hunk_line.starts_with("+++")
                    {
                        break;
                    }
                    if hunk_line.starts_with('+') || hunk_line.starts_with('-') || hunk_line.starts_with(' ') || hunk_line.is_empty() {
                        hunk_lines.push(hunk_line.to_string());
                    }
                    i += 1;
                }

                current_hunks.push(DiffHunk {
                    old_start,
                    old_count,
                    new_start,
                    new_count,
                    lines: hunk_lines,
                });
                continue;
            } else {
                return Err(format!("Malformed hunk header: {}", line));
            }
        }

        i += 1;
    }

    if let Some(ref file_path) = current_file {
        if !current_hunks.is_empty() {
            files.insert(file_path.clone(), current_hunks);
        }
    }

    Ok(files)
}

pub fn apply_hunk_to_lines(lines: &[String], hunk: &DiffHunk) -> Vec<String> {
    let mut start_idx = if hunk.old_start > 0 { hunk.old_start - 1 } else { 0 };
    if start_idx > lines.len() {
        start_idx = lines.len();
    }

    let mut new_section = Vec::new();
    for hunk_line in &hunk.lines {
        if hunk_line.starts_with('+') {
            new_section.push(hunk_line[1..].to_string());
        } else if hunk_line.starts_with(' ') {
            new_section.push(hunk_line[1..].to_string());
        } else if hunk_line.is_empty() {
            new_section.push(String::new());
        }
    }

    let mut end_idx = start_idx + hunk.old_count;
    if end_idx > lines.len() {
        end_idx = lines.len();
    }

    let mut result = Vec::new();
    result.extend_from_slice(&lines[..start_idx]);
    result.extend(new_section);
    result.extend_from_slice(&lines[end_idx..]);
    result
}
