use serde::{Serialize, Deserialize};
use regex::Regex;
use std::collections::{HashMap, HashSet};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SymbolSpan {
    pub symbol_id: String,
    pub start_line: usize,
    pub end_line: usize,
}

pub fn extract_symbol_spans(path: &str, source_code: &str) -> Vec<SymbolSpan> {
    let ext = match path.rfind('.') {
        Some(idx) => &path[idx..],
        None => "",
    };

    match ext {
        ".js" | ".jsx" | ".ts" | ".tsx" | ".mjs" | ".cjs" => {
            _regex_extract_js_ts(source_code)
        }
        ".go" => {
            _regex_extract_go(source_code)
        }
        ".java" => {
            _regex_extract_java(source_code)
        }
        ".rs" => {
            _regex_extract_rust(source_code)
        }
        _ => Vec::new(),
    }
}

pub fn find_symbol_at_line(spans: &[SymbolSpan], line_num: usize) -> Option<String> {
    let mut candidates: Vec<&SymbolSpan> = spans
        .iter()
        .filter(|s| line_num >= s.start_line && line_num <= s.end_line)
        .collect();

    if candidates.is_empty() {
        return None;
    }

    // Return the most specific (shortest) span
    candidates.sort_by_key(|s| s.end_line - s.start_line);
    Some(candidates[0].symbol_id.clone())
}

pub fn decode_affected_symbols(raw_symbols: Option<&str>) -> Option<HashMap<String, HashSet<String>>> {
    let raw = raw_symbols?;
    let values: Vec<String> = serde_json::from_str(raw).ok()?;
    
    let mut by_path: HashMap<String, HashSet<String>> = HashMap::new();
    for value in values {
        if let Some(idx) = value.find("::") {
            let path = &value[..idx];
            let symbol_id = &value[idx + 2..];
            if !path.is_empty() && !symbol_id.is_empty() {
                by_path.entry(path.to_string()).or_default().insert(symbol_id.to_string());
            }
        }
    }
    Some(by_path)
}

pub fn has_semantic_conflict(
    proposal_paths: &HashSet<String>,
    proposal_symbols: Option<&HashMap<String, HashSet<String>>>,
    other_paths: &HashSet<String>,
    other_symbols: Option<&HashMap<String, HashSet<String>>>,
) -> bool {
    let common_paths: HashSet<&String> = proposal_paths.intersection(other_paths).collect();
    if common_paths.is_empty() {
        return false;
    }

    for path in common_paths {
        let p_syms = match proposal_symbols {
            Some(map) => match map.get(path.as_str()) {
                Some(set) => set,
                None => return true,
            },
            None => return true,
        };

        let o_syms = match other_symbols {
            Some(map) => match map.get(path.as_str()) {
                Some(set) => set,
                None => return true,
            },
            None => return true,
        };

        if p_syms.contains("__file__") || p_syms.contains("__raw__")
            || o_syms.contains("__file__") || o_syms.contains("__raw__")
        {
            return true;
        }

        if p_syms.intersection(o_syms).next().is_some() {
            return true;
        }
    }

    false
}

pub fn check_overlap(p1: &models::entities::Proposal, p2: &models::entities::Proposal) -> bool {
    let p1_paths: HashSet<String> = serde_json::from_str(&p1.affected_paths).unwrap_or_default();
    let p2_paths: HashSet<String> = serde_json::from_str(&p2.affected_paths).unwrap_or_default();
    let p1_symbols = decode_affected_symbols(p1.affected_symbols.as_deref());
    let p2_symbols = decode_affected_symbols(p2.affected_symbols.as_deref());
    has_semantic_conflict(&p1_paths, p1_symbols.as_ref(), &p2_paths, p2_symbols.as_ref())
}

pub fn compute_affected_symbols(
    change_type: &str,
    change_content: &str,
    affected_paths: &[String],
    doc_store: &crate::document_store::DocumentStore,
) -> Vec<String> {
    use std::collections::HashSet;
    let mut symbols = HashSet::new();
    let mut wildcard_paths = HashSet::new();

    if change_type == "unified_diff" {
        let file_hunks = crate::document_store::parse_unified_diff(change_content).unwrap_or_default();
        for path in affected_paths {
            let hunks = match file_hunks.get(path) {
                Some(h) => h,
                None => {
                    wildcard_paths.insert(path.clone());
                    continue;
                }
            };
            if hunks.is_empty() {
                wildcard_paths.insert(path.clone());
                continue;
            }

            let doc_content = doc_store.documents.get(path)
                .map(|d| d.document.get_text())
                .unwrap_or_default();

            if path.ends_with(".py") {
                let mut structured = crate::structured_doc::StructuredDocument::new("temp".to_string());
                structured.set_content(&doc_content);

                let get_symbol_at = |line: usize| -> Option<String> {
                    structured.get_node_at_line(line)
                };

                let mut is_wildcard = false;
                for hunk in hunks {
                    if hunk.old_start <= 0 {
                        is_wildcard = true;
                        break;
                    }
                    if hunk.old_count == 0 {
                        if let Some(sym) = get_symbol_at(hunk.old_start) {
                            symbols.insert(format!("{}::{}", path, sym));
                        } else {
                            is_wildcard = true;
                            break;
                        }
                    } else {
                        if let Some(first_symbol) = get_symbol_at(hunk.old_start) {
                            let mut multi_node = false;
                            for line in (hunk.old_start + 1)..(hunk.old_start + hunk.old_count) {
                                if get_symbol_at(line) != Some(first_symbol.clone()) {
                                    multi_node = true;
                                    break;
                                }
                            }
                            if multi_node {
                                is_wildcard = true;
                                break;
                            } else {
                                symbols.insert(format!("{}::{}", path, first_symbol));
                            }
                        } else {
                            is_wildcard = true;
                            break;
                        }
                    }
                }
                if is_wildcard {
                    wildcard_paths.insert(path.clone());
                }
            } else {
                let spans = extract_symbol_spans(path, &doc_content);
                if spans.is_empty() {
                    wildcard_paths.insert(path.clone());
                    continue;
                }

                let get_symbol_at = |line: usize| -> Option<String> {
                    find_symbol_at_line(&spans, line)
                };

                let mut is_wildcard = false;
                for hunk in hunks {
                    if hunk.old_start <= 0 {
                        is_wildcard = true;
                        break;
                    }
                    if hunk.old_count == 0 {
                        if let Some(sym) = get_symbol_at(hunk.old_start) {
                            symbols.insert(format!("{}::{}", path, sym));
                        } else {
                            is_wildcard = true;
                            break;
                        }
                    } else {
                        if let Some(first_symbol) = get_symbol_at(hunk.old_start) {
                            let mut multi_node = false;
                            for line in (hunk.old_start + 1)..(hunk.old_start + hunk.old_count) {
                                if get_symbol_at(line) != Some(first_symbol.clone()) {
                                    multi_node = true;
                                    break;
                                }
                            }
                            if multi_node {
                                is_wildcard = true;
                                break;
                            } else {
                                symbols.insert(format!("{}::{}", path, first_symbol));
                            }
                        } else {
                            is_wildcard = true;
                            break;
                        }
                    }
                }
                if is_wildcard {
                    wildcard_paths.insert(path.clone());
                }
            }
        }
    } else {
        // full_file or any other fallback
        for path in affected_paths {
            wildcard_paths.insert(path.clone());
        }
    }

    for path in wildcard_paths {
        symbols.insert(format!("{}::__file__", path));
        symbols.insert(format!("{}::__raw__", path));
    }

    let mut res: Vec<String> = symbols.into_iter().collect();
    res.sort();
    res
}

fn _dedupe_and_order_spans(mut spans: Vec<SymbolSpan>) -> Vec<SymbolSpan> {
    if spans.is_empty() {
        return Vec::new();
    }

    // Sort by start_line, then end_line, then symbol_id
    spans.sort_by(|a, b| {
        a.start_line.cmp(&b.start_line)
            .then_with(|| a.end_line.cmp(&b.end_line))
            .then_with(|| a.symbol_id.cmp(&b.symbol_id))
    });

    let mut result = Vec::new();
    let mut seen = HashMap::new();

    for span in spans {
        let count = seen.entry(span.symbol_id.clone()).or_insert(0);
        *count += 1;
        let symbol_id = if *count == 1 {
            span.symbol_id.clone()
        } else {
            format!("{}#{}", span.symbol_id, *count)
        };
        result.push(SymbolSpan {
            symbol_id,
            start_line: span.start_line,
            end_line: span.end_line,
        });
    }

    result
}

fn _spans_from_starts(lines: &[&str], starts: Vec<(usize, String)>) -> Vec<SymbolSpan> {
    if starts.is_empty() {
        return Vec::new();
    }

    let mut spans = Vec::new();
    for (idx, (start_line, symbol_id)) in starts.iter().enumerate() {
        let end_line = if idx + 1 < starts.len() {
            starts[idx + 1].0 - 1
        } else {
            lines.len()
        };
        let end_line = if end_line < *start_line {
            *start_line
        } else {
            end_line
        };
        spans.push(SymbolSpan {
            symbol_id: symbol_id.clone(),
            start_line: *start_line,
            end_line,
        });
    }

    _dedupe_and_order_spans(spans)
}

fn _regex_extract_js_ts(source_code: &str) -> Vec<SymbolSpan> {
    let lines: Vec<&str> = source_code.lines().collect();
    if lines.is_empty() {
        return Vec::new();
    }

    let fn_pattern = Regex::new(
        r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\("
    ).unwrap();
    let class_pattern = Regex::new(
        r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)\b"
    ).unwrap();
    let const_fn_pattern = Regex::new(
        r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?\([^)]*\)\s*=>"
    ).unwrap();

    let mut starts = Vec::new();
    for (i, line) in lines.iter().enumerate() {
        let line_num = i + 1;
        if let Some(caps) = fn_pattern.captures(line) {
            starts.push((line_num, format!("FUNC:{}", &caps[1])));
            continue;
        }
        if let Some(caps) = const_fn_pattern.captures(line) {
            starts.push((line_num, format!("FUNC:{}", &caps[1])));
            continue;
        }
        if let Some(caps) = class_pattern.captures(line) {
            starts.push((line_num, format!("CLASS:{}", &caps[1])));
        }
    }

    _spans_from_starts(&lines, starts)
}

fn _regex_extract_go(source_code: &str) -> Vec<SymbolSpan> {
    let lines: Vec<&str> = source_code.lines().collect();
    if lines.is_empty() {
        return Vec::new();
    }

    let fn_pattern = Regex::new(
        r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\("
    ).unwrap();
    let type_pattern = Regex::new(
        r"^\s*type\s+([A-Za-z_]\w*)\s+(?:struct|interface|\w+)"
    ).unwrap();

    let mut starts = Vec::new();
    for (i, line) in lines.iter().enumerate() {
        let line_num = i + 1;
        if let Some(caps) = fn_pattern.captures(line) {
            starts.push((line_num, format!("FUNC:{}", &caps[1])));
            continue;
        }
        if let Some(caps) = type_pattern.captures(line) {
            starts.push((line_num, format!("TYPE:{}", &caps[1])));
        }
    }

    _spans_from_starts(&lines, starts)
}

fn _regex_extract_java(source_code: &str) -> Vec<SymbolSpan> {
    let lines: Vec<&str> = source_code.lines().collect();
    if lines.is_empty() {
        return Vec::new();
    }

    let type_pattern = Regex::new(
        r"^\s*(?:public|private|protected)?\s*(?:abstract|final)?\s*(class|interface|enum)\s+([A-Za-z_]\w*)\b"
    ).unwrap();
    let method_pattern = Regex::new(
        r"^\s*(?:public|private|protected)?\s*(?:static\s+)?(?:final\s+)?(?:[A-Za-z_][\w<>,\[\]]*\s+)+([A-Za-z_]\w*)\s*\("
    ).unwrap();

    let mut starts = Vec::new();
    let reserved = ["if", "for", "while", "switch", "catch"];

    for (i, line) in lines.iter().enumerate() {
        let line_num = i + 1;
        if let Some(caps) = type_pattern.captures(line) {
            starts.push((line_num, format!("CLASS:{}", &caps[2])));
            continue;
        }
        if let Some(caps) = method_pattern.captures(line) {
            let m_name = &caps[1];
            if !reserved.contains(&m_name) {
                starts.push((line_num, format!("METHOD:{}", m_name)));
            }
        }
    }

    _spans_from_starts(&lines, starts)
}

fn _regex_extract_rust(source_code: &str) -> Vec<SymbolSpan> {
    let lines: Vec<&str> = source_code.lines().collect();
    if lines.is_empty() {
        return Vec::new();
    }

    let fn_pattern = Regex::new(
        r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)\s*\("
    ).unwrap();
    let type_pattern = Regex::new(
        r"^\s*(?:pub\s+)?(?:struct|enum|trait)\s+([A-Za-z_]\w*)\b"
    ).unwrap();
    let impl_pattern = Regex::new(
        r"^\s*impl\s+([A-Za-z_]\w*)\b"
    ).unwrap();

    let mut starts = Vec::new();
    for (i, line) in lines.iter().enumerate() {
        let line_num = i + 1;
        if let Some(caps) = fn_pattern.captures(line) {
            starts.push((line_num, format!("FUNC:{}", &caps[1])));
            continue;
        }
        if let Some(caps) = type_pattern.captures(line) {
            starts.push((line_num, format!("TYPE:{}", &caps[1])));
            continue;
        }
        if let Some(caps) = impl_pattern.captures(line) {
            starts.push((line_num, format!("TYPE:{}", &caps[1])));
        }
    }

    _spans_from_starts(&lines, starts)
}
