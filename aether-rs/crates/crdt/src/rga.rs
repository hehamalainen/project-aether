use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::time::{SystemTime, UNIX_EPOCH};

tokio::task_local! {
    pub static RGA_ALIGNMENT_TIME_NS: std::sync::atomic::AtomicU64;
}



#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub enum OperationType {
    INSERT,
    DELETE,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Hash)]
pub struct ElementId {
    pub replica_id: String,
    pub sequence: i64,
}

// Ord implementation must match Python's: by sequence, then by replica_id
impl PartialOrd for ElementId {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for ElementId {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        if self.sequence != other.sequence {
            self.sequence.cmp(&other.sequence)
        } else {
            self.replica_id.cmp(&other.replica_id)
        }
    }
}

impl ElementId {
    pub fn root() -> Self {
        Self {
            replica_id: "_root".to_string(),
            sequence: 0,
        }
    }

    pub fn to_string_key(&self) -> String {
        format!("{}:{}", self.replica_id, self.sequence)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RGAElement {
    pub id: ElementId,
    pub value: String,
    pub deleted: bool,
    pub after_id: Option<ElementId>,
    #[serde(default)]
    pub deleted_by: Option<crate::causality::Dot>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RGAOperation {
    pub op_type: OperationType,
    pub element_id: ElementId,
    pub value: Option<String>,
    pub after_id: Option<ElementId>,
    pub timestamp: f64,
    #[serde(default)]
    pub deleted_by: Option<crate::causality::Dot>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RGADocument {
    pub replica_id: String,
    pub elements: HashMap<String, RGAElement>,
    pub order: Vec<ElementId>,
    pub sequence: i64,
    pub operations: Vec<RGAOperation>,
    pub pending_inserts: HashMap<String, Vec<RGAOperation>>,
    pub known_deletions: HashSet<String>,
    /// Exact interval-compressed membership: replica_id → sorted closed intervals of vacuumed sequences.
    #[serde(default)]
    pub vacuumed_intervals: BTreeMap<String, Vec<(i64, i64)>>,
    /// Exact parent redirect: vacuumed element key → surviving ancestor ElementId (or None for root).
    #[serde(default)]
    pub vacuumed_redirects: HashMap<String, Option<ElementId>>,
}

impl RGADocument {
    pub fn new(replica_id: String) -> Self {
        let mut doc = Self {
            replica_id,
            elements: HashMap::new(),
            order: Vec::new(),
            sequence: 0,
            operations: Vec::new(),
            pending_inserts: HashMap::new(),
            known_deletions: HashSet::new(),
            vacuumed_intervals: BTreeMap::new(),
            vacuumed_redirects: HashMap::new(),
        };
        let root = ElementId::root();
        doc.elements.insert(
            root.to_string_key(),
            RGAElement {
                id: root,
                value: "".to_string(),
                deleted: false,
                after_id: None,
                deleted_by: None,
            },
        );
        doc
    }

    fn next_id(&mut self) -> ElementId {
        self.sequence += 1;
        ElementId {
            replica_id: self.replica_id.clone(),
            sequence: self.sequence,
        }
    }

    fn find_position(&self, index: usize) -> (usize, Option<ElementId>) {
        let mut visible_count = 0;
        for (i, eid) in self.order.iter().enumerate() {
            if let Some(elem) = self.elements.get(&eid.to_string_key()) {
                if !elem.deleted {
                    if visible_count == index {
                        return (i, Some(eid.clone()));
                    }
                    visible_count += 1;
                }
            }
        }
        (self.order.len(), None)
    }

    fn rebuild_order(&mut self) {
        let mut children: HashMap<ElementId, Vec<ElementId>> = HashMap::new();
        let root_key = ElementId::root().to_string_key();

        for (eid_str, elem) in &self.elements {
            if eid_str == &root_key {
                continue;
            }
            let parent = elem.after_id.clone().unwrap_or_else(ElementId::root);
            children.entry(parent).or_default().push(elem.id.clone());
        }

        // Sort siblings in ascending order.
        // When pushed to the LIFO stack in this order, siblings with larger IDs
        // will be at the top of the stack and popped/traversed first.
        for siblings in children.values_mut() {
            siblings.sort();
        }

        let mut ordered: Vec<ElementId> = Vec::new();
        let mut seen: HashSet<String> = HashSet::new();
        let mut stack: Vec<ElementId> = vec![ElementId::root()];

        while let Some(parent) = stack.pop() {
            let parent_key = parent.to_string_key();
            if parent_key != root_key {
                ordered.push(parent.clone());
            }

            if let Some(siblings) = children.get(&parent) {
                for sibling in siblings {
                    let sibling_key = sibling.to_string_key();
                    if seen.contains(&sibling_key) {
                        continue;
                    }
                    seen.insert(sibling_key);
                    stack.push(sibling.clone());
                }
            }
        }

        // Recover unreachable nodes
        if ordered.len() < self.elements.len() - 1 {
            let mut remaining: Vec<ElementId> = self
                .elements
                .iter()
                .filter(|(eid_str, _)| *eid_str != &root_key && !seen.contains(*eid_str))
                .map(|(_, elem)| elem.id.clone())
                .collect();
            remaining.sort();
            ordered.extend(remaining);
        }

        self.order = ordered;
    }

    fn queue_pending_insert(&mut self, op: &RGAOperation) {
        if let Some(after_id) = &op.after_id {
            let key = after_id.to_string_key();
            let pending = self.pending_inserts.entry(key).or_default();
            if !pending
                .iter()
                .any(|existing| existing.element_id == op.element_id)
            {
                pending.push(op.clone());
            }
        }
    }

    fn drain_pending_inserts(&mut self, parent_id: &ElementId, defer_rebuild: bool) {
        let key = parent_id.to_string_key();
        if let Some(pending) = self.pending_inserts.remove(&key) {
            for op in pending {
                self.apply_operation_internal(&op, false, defer_rebuild);
            }
        }
    }

    fn get_timestamp() -> f64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or(std::time::Duration::from_secs(0))
            .as_secs_f64()
    }

    pub fn local_insert(&mut self, position: usize, char: String) -> RGAOperation {
        let after_id = if position == 0 {
            ElementId::root()
        } else {
            let (_, found_id) = self.find_position(position - 1);
            found_id.unwrap_or_else(|| self.order.last().cloned().unwrap_or_else(ElementId::root))
        };

        let new_id = self.next_id();
        let op = RGAOperation {
            op_type: OperationType::INSERT,
            element_id: new_id,
            value: Some(char),
            after_id: Some(after_id),
            timestamp: Self::get_timestamp(),
            deleted_by: None,
        };

        self.apply_operation(&op);
        op
    }

    pub fn local_delete_bounded(&mut self, position: usize, active_dot: Option<&crate::causality::Dot>) -> Option<RGAOperation> {
        let (_, eid) = self.find_position(position);
        if let Some(element_id) = eid {
            let op = RGAOperation {
                op_type: OperationType::DELETE,
                element_id,
                value: None,
                after_id: None,
                timestamp: Self::get_timestamp(),
                deleted_by: active_dot.cloned(),
            };
            self.apply_operation(&op);
            Some(op)
        } else {
            None
        }
    }

    pub fn local_delete(&mut self, position: usize) -> Option<RGAOperation> {
        self.local_delete_bounded(position, None)
    }

    pub fn apply_operation(&mut self, op: &RGAOperation) -> bool {
        self.apply_operation_internal(op, true, false)
    }

    fn apply_operation_internal(&mut self, op: &RGAOperation, push_to_ops: bool, defer_rebuild: bool) -> bool {
        let eid_str = op.element_id.to_string_key();

        // Anti-resurrection: reject operations on elements already vacuumed
        if self.is_vacuumed(&op.element_id.replica_id, op.element_id.sequence) {
            return false;
        }

        // Missing-parent redirect: rewrite after_id if parent was vacuumed
        if let Some(ref after_id) = op.after_id {
            let after_key = after_id.to_string_key();
            if self.vacuumed_redirects.contains_key(&after_key) {
                let resolved = self.vacuumed_redirects.get(&after_key).cloned().flatten();
                let mut rewritten = op.clone();
                rewritten.after_id = resolved;
                return self.apply_operation_internal(&rewritten, push_to_ops, defer_rebuild);
            }
        }

        match op.op_type {
            OperationType::INSERT => {
                if self.elements.contains_key(&eid_str) {
                    return false;
                }

                if let Some(after_id) = &op.after_id {
                    if *after_id != ElementId::root()
                        && !self.elements.contains_key(&after_id.to_string_key())
                    {
                        self.queue_pending_insert(op);
                        if push_to_ops {
                            self.operations.push(op.clone());
                        }
                        return false;
                    }
                }

                let is_deleted = self.known_deletions.contains(&eid_str)
                    || self.is_vacuumed(&op.element_id.replica_id, op.element_id.sequence);

                let elem = RGAElement {
                    id: op.element_id.clone(),
                    value: op.value.clone().unwrap_or_default(),
                    deleted: is_deleted,
                    after_id: op.after_id.clone(),
                    deleted_by: op.deleted_by.clone(),
                };
                self.elements.insert(eid_str.clone(), elem);
                if !defer_rebuild {
                    self.rebuild_order();
                }

                let element_id_clone = op.element_id.clone();
                self.drain_pending_inserts(&element_id_clone, defer_rebuild);

                if op.element_id.replica_id == self.replica_id {
                    self.sequence = self.sequence.max(op.element_id.sequence);
                }
            }
            OperationType::DELETE => {
                let already_deleted;
                if let Some(elem) = self.elements.get_mut(&eid_str) {
                    already_deleted = elem.deleted;
                    elem.deleted = true;
                    if op.deleted_by.is_some() {
                        elem.deleted_by = op.deleted_by.clone();
                    }
                } else {
                    already_deleted = self.known_deletions.contains(&eid_str);
                    self.known_deletions.insert(eid_str.clone());
                }
                if already_deleted {
                    return false;
                }
            }
        }

        if push_to_ops {
            self.operations.push(op.clone());
        }
        true
    }

    pub fn apply_operations(&mut self, ops: &[RGAOperation]) -> usize {
        let mut applied = 0;
        let mut need_rebuild = false;
        for op in ops {
            if self.apply_operation_internal(op, true, true) {
                applied += 1;
                if op.op_type == OperationType::INSERT {
                    need_rebuild = true;
                }
            }
        }
        if need_rebuild {
            self.rebuild_order();
        }
        applied
    }

    pub fn get_text(&self) -> String {
        let mut result = String::new();
        for eid in &self.order {
            if let Some(elem) = self.elements.get(&eid.to_string_key()) {
                if !elem.deleted && !elem.value.is_empty() {
                    result.push_str(&elem.value);
                }
            }
        }
        result
    }

    pub fn get_length(&self) -> usize {
        let mut count = 0;
        for eid in &self.order {
            if let Some(elem) = self.elements.get(&eid.to_string_key()) {
                if !elem.deleted {
                    count += 1;
                }
            }
        }
        count
    }

    pub fn replace_text(&mut self, text: &str) -> Vec<RGAOperation> {
        let root = ElementId::root();
        let mut elements: HashMap<String, RGAElement> = HashMap::new();
        elements.insert(
            root.to_string_key(),
            RGAElement {
                id: root.clone(),
                value: "".to_string(),
                deleted: false,
                after_id: None,
                deleted_by: None,
            },
        );

        let mut order: Vec<ElementId> = Vec::new();
        let mut ops: Vec<RGAOperation> = Vec::new();

        let mut prev_id = root;
        let mut seq = self.sequence;

        // Python code iterates over characters in string
        for ch in text.chars() {
            seq += 1;
            let eid = ElementId {
                replica_id: self.replica_id.clone(),
                sequence: seq,
            };

            elements.insert(
                eid.to_string_key(),
                RGAElement {
                    id: eid.clone(),
                    value: ch.to_string(),
                    deleted: false,
                    after_id: Some(prev_id.clone()),
                    deleted_by: None,
                },
            );

            order.push(eid.clone());
            ops.push(RGAOperation {
                op_type: OperationType::INSERT,
                element_id: eid.clone(),
                value: Some(ch.to_string()),
                after_id: Some(prev_id.clone()),
                timestamp: Self::get_timestamp(),
                deleted_by: None,
            });

            prev_id = eid;
        }

        self.elements = elements;
        self.order = order;
        self.sequence = seq;
        self.operations = ops.clone();
        self.pending_inserts.clear();
        self.known_deletions.clear();
        self.vacuumed_intervals.clear();
        self.vacuumed_redirects.clear();
        ops
    }

    #[allow(clippy::needless_range_loop)]
    pub fn replace_tokens_bounded(&mut self, tokens: &[String], active_dot: Option<&crate::causality::Dot>) -> Vec<RGAOperation> {
        let start = std::time::Instant::now();
        // 1. Extract active (non-deleted) elements and their values
        let active_elements: Vec<(ElementId, String)> = self.order
            .iter()
            .filter_map(|eid| {
                let elem = self.elements.get(&eid.to_string_key())?;
                if !elem.deleted {
                    Some((eid.clone(), elem.value.clone()))
                } else {
                    None
                }
            })
            .collect();

        let n = active_elements.len();
        let m = tokens.len();

        // 2. Strip shared prefix and suffix to isolate the changed region.
        //    This reduces the Wagner-Fischer DP from O(N*M) to O(K*L) where
        //    K, L are the sizes of the differing region.
        let mut prefix_len = 0;
        while prefix_len < n && prefix_len < m
            && active_elements[prefix_len].1 == tokens[prefix_len]
        {
            prefix_len += 1;
        }

        // Strip suffix (from the end, not overlapping the prefix)
        let mut suffix_len = 0;
        while suffix_len < (n - prefix_len) && suffix_len < (m - prefix_len)
            && active_elements[n - 1 - suffix_len].1 == tokens[m - 1 - suffix_len]
        {
            suffix_len += 1;
        }

        // 3. Apply one-token overlap margin on each side of the stripped region.
        //    This ensures boundary tokens cannot yield sub-optimal alignment
        //    across the strip seam.
        let overlap = 1;
        let window_start = prefix_len.saturating_sub(overlap);
        let window_end_active = if suffix_len >= overlap {
            n - suffix_len + overlap
        } else {
            n
        };
        let window_end_tokens = if suffix_len >= overlap {
            m - suffix_len + overlap
        } else {
            m
        };

        // Clamp to valid ranges
        let window_end_active = window_end_active.min(n);
        let window_end_tokens = window_end_tokens.min(m);

        // 4. Ambiguity check: if the overlap tokens don't match between active
        //    and new, the strip boundaries are unreliable. Fall back to full
        //    alignment to avoid sub-optimal results.
        let use_full_alignment = if prefix_len == 0 && suffix_len == 0 {
            // No stripping possible — already full alignment
            false
        } else {
            // Check prefix overlap token (if we pulled it into the window)
            let prefix_overlap_mismatch = window_start < prefix_len
                && window_start < n && window_start < m
                && active_elements[window_start].1 != tokens[window_start];
            // Check suffix overlap token
            let suffix_overlap_mismatch = window_end_active > (n - suffix_len)
                && (window_end_active - 1) < n && (window_end_tokens - 1) < m
                && active_elements[window_end_active - 1].1 != tokens[window_end_tokens - 1];
            prefix_overlap_mismatch || suffix_overlap_mismatch
        };

        let (eff_start, eff_end_active, eff_end_tokens) = if use_full_alignment {
            (0, n, m)
        } else {
            (window_start, window_end_active, window_end_tokens)
        };

        // The windowed slices for alignment
        let active_window = &active_elements[eff_start..eff_end_active];
        let token_window = &tokens[eff_start..eff_end_tokens];
        let wn = active_window.len();
        let wm = token_window.len();

        // 5. Wagner-Fischer dynamic programming on the windowed region
        let mut dp = vec![vec![0usize; wm + 1]; wn + 1];

        for i in 0..=wn {
            dp[i][0] = i;
        }
        for j in 0..=wm {
            dp[0][j] = j;
        }

        for i in 1..=wn {
            for j in 1..=wm {
                if active_window[i - 1].1 == token_window[j - 1] {
                    dp[i][j] = dp[i - 1][j - 1];
                } else {
                    let cost_del = dp[i - 1][j] + 1;
                    let cost_ins = dp[i][j - 1] + 1;
                    dp[i][j] = cost_del.min(cost_ins);
                }
            }
        }

        // 6. Backtrack to find the sequence of steps
        #[derive(Debug, Clone, PartialEq, Eq)]
        enum Step {
            Keep { active_idx: usize },
            Delete { active_idx: usize },
            Insert { token_idx: usize },
        }

        let mut i = wn;
        let mut j = wm;
        let mut steps = Vec::new();

        while i > 0 || j > 0 {
            // Tie-break: prefer insertion (moving left in the DP table) over matching/deletion
            // if the insertion cost matches the current cell cost. This prevents matching
            // a token to a far-away duplicate (like a newline) and shifting active elements.
            if j > 0 && dp[i][j] == dp[i][j - 1] + 1 {
                steps.push(Step::Insert { token_idx: eff_start + j - 1 });
                j -= 1;
            } else if i > 0 && j > 0 && active_window[i - 1].1 == token_window[j - 1] && dp[i][j] == dp[i - 1][j - 1] {
                steps.push(Step::Keep { active_idx: eff_start + i - 1 });
                i -= 1;
                j -= 1;
            } else if i > 0 && dp[i][j] == dp[i - 1][j] + 1 {
                steps.push(Step::Delete { active_idx: eff_start + i - 1 });
                i -= 1;
            } else {
                // Fallback mismatch/substitution
                if i > 0 && j > 0 {
                    steps.push(Step::Delete { active_idx: eff_start + i - 1 });
                    steps.push(Step::Insert { token_idx: eff_start + j - 1 });
                    i -= 1;
                    j -= 1;
                } else if i > 0 {
                    steps.push(Step::Delete { active_idx: eff_start + i - 1 });
                    i -= 1;
                } else {
                    steps.push(Step::Insert { token_idx: eff_start + j - 1 });
                    j -= 1;
                }
            }
        }

        steps.reverse();

        // 7. Prepend Keep steps for the prefix (elements before eff_start)
        let mut all_steps = Vec::new();
        for idx in 0..eff_start {
            all_steps.push(Step::Keep { active_idx: idx });
        }
        all_steps.extend(steps);
        // Append Keep steps for the suffix (elements after eff_end_active)
        for idx in eff_end_active..n {
            all_steps.push(Step::Keep { active_idx: idx });
        }

        // 8. Generate operations
        let mut generated_ops = Vec::new();
        let mut prev_id = ElementId::root();

        for step in all_steps {
            match step {
                Step::Keep { active_idx } => {
                    prev_id = active_elements[active_idx].0.clone();
                }
                Step::Delete { active_idx } => {
                    let id_to_delete = active_elements[active_idx].0.clone();
                    let op = RGAOperation {
                        op_type: OperationType::DELETE,
                        element_id: id_to_delete.clone(),
                        value: None,
                        after_id: None,
                        timestamp: Self::get_timestamp(),
                        deleted_by: active_dot.cloned(),
                    };
                    self.apply_operation(&op);
                    generated_ops.push(op);
                }
                Step::Insert { token_idx } => {
                    let next_id = self.next_id();
                    let op = RGAOperation {
                        op_type: OperationType::INSERT,
                        element_id: next_id.clone(),
                        value: Some(tokens[token_idx].clone()),
                        after_id: Some(prev_id.clone()),
                        timestamp: Self::get_timestamp(),
                        deleted_by: None,
                    };
                    self.apply_operation(&op);
                    generated_ops.push(op);
                    prev_id = next_id;
                }
            }
        }

        let elapsed = start.elapsed().as_nanos() as u64;
        let _ = RGA_ALIGNMENT_TIME_NS.try_with(|cell| {
            cell.fetch_add(elapsed, std::sync::atomic::Ordering::SeqCst);
        });

        generated_ops
    }

    pub fn replace_tokens(&mut self, tokens: &[String]) -> Vec<RGAOperation> {
        self.replace_tokens_bounded(tokens, None)
    }

    /// Insert a closed interval [lo, hi] into a sorted interval list, merging overlapping
    /// or adjacent intervals.
    fn add_interval(intervals: &mut Vec<(i64, i64)>, lo: i64, hi: i64) {
        // Find the range of existing intervals that overlap or are adjacent to [lo, hi]
        let mut merged_lo = lo;
        let mut merged_hi = hi;
        let mut first = intervals.len();
        let mut last = 0;

        for (i, &(s, e)) in intervals.iter().enumerate() {
            // Overlapping or adjacent: [s, e] touches [lo-1, hi+1]
            if s <= hi + 1 && e >= lo - 1 {
                if first > i {
                    first = i;
                }
                last = i + 1;
                merged_lo = merged_lo.min(s);
                merged_hi = merged_hi.max(e);
            }
        }

        if first < last {
            // Replace the range [first..last) with the merged interval
            intervals.splice(first..last, std::iter::once((merged_lo, merged_hi)));
        } else {
            // No overlap found; insert in sorted position
            let pos = intervals.partition_point(|&(s, _)| s < lo);
            intervals.insert(pos, (lo, hi));
        }
    }

    /// Check if an element with the given replica_id and sequence has been vacuumed.
    pub fn is_vacuumed(&self, replica_id: &str, sequence: i64) -> bool {
        if let Some(intervals) = self.vacuumed_intervals.get(replica_id) {
            intervals
                .iter()
                .any(|&(s, e)| sequence >= s && sequence <= e)
        } else {
            false
        }
    }

    /// Purge all deleted (tombstoned) elements from memory, building exact
    /// `vacuumed_intervals` and `vacuumed_redirects` summaries.
    ///
    /// This is the code path for **explicit local compaction**. Elements may
    /// also be physically removed during `merge()` step 1b when imported
    /// vacuum knowledge covers local elements (Option A — strict physical
    /// exclusivity).
    ///
    /// After this call:
    /// - No `deleted == true` elements remain in `self.elements` (except root).
    /// - `vacuumed_intervals` provides exact membership for anti-resurrection checks.
    /// - `vacuumed_redirects` provides exact parent-chain resolution for stale operations.
    /// - `known_deletions` entries for vacuumed elements are removed (exclusivity).
    /// - `operations` log is pruned of vacuumed element ops, with surviving ops' after_id rewritten.
    pub fn vacuum_tombstones_bounded(&mut self, meet: Option<&crate::causality::VersionVector>) {
        let root_key = ElementId::root().to_string_key();

        // 1. Collect tombstone set T, applying meet constraint if provided
        let tombstone_keys: HashSet<String> = self
            .elements
            .iter()
            .filter(|(key, elem)| {
                if !elem.deleted || *key == &root_key {
                    return false;
                }
                if let Some(meet_vv) = meet {
                    if let Some(ref dot) = elem.deleted_by {
                        meet_vv.includes(dot)
                    } else {
                        false // Legacy tombstone without deleted_by: do not vacuum under bounded vacuum
                    }
                } else {
                    true // Unbounded vacuum: vacuum all tombstones
                }
            })
            .map(|(key, _)| key.clone())
            .collect();

        if tombstone_keys.is_empty() {
            return;
        }

        // 2. Compute redirect map: for each tombstone, walk its after_id chain
        //    upward to the first non-tombstone ancestor (transitive within this pass).
        let mut redirects: HashMap<String, Option<ElementId>> = HashMap::new();
        for tombstone_key in &tombstone_keys {
            let resolved = self.resolve_ancestor(tombstone_key, &tombstone_keys);
            redirects.insert(tombstone_key.clone(), resolved);
        }

        // 3. Rewrite active element parent pointers
        for (key, elem) in self.elements.iter_mut() {
            if tombstone_keys.contains(key) || *key == root_key {
                continue;
            }
            if let Some(ref after_id) = elem.after_id {
                let after_key = after_id.to_string_key();
                if let Some(resolved) = redirects.get(&after_key) {
                    elem.after_id = resolved.clone();
                }
            }
        }

        // 4. Rewrite operations log
        self.operations.retain(|op| {
            !tombstone_keys.contains(&op.element_id.to_string_key())
        });
        for op in self.operations.iter_mut() {
            if let Some(ref after_id) = op.after_id {
                let after_key = after_id.to_string_key();
                if let Some(resolved) = redirects.get(&after_key) {
                    op.after_id = resolved.clone();
                }
            }
        }

        // 5. Rewrite pending_inserts: re-key queues whose parent is being vacuumed
        let stranded_keys: Vec<String> = self
            .pending_inserts
            .keys()
            .filter(|k| tombstone_keys.contains(*k))
            .cloned()
            .collect();
        for stranded_key in stranded_keys {
            if let Some(ops) = self.pending_inserts.remove(&stranded_key) {
                let resolved_parent = redirects.get(&stranded_key).cloned().flatten();
                for mut op in ops {
                    op.after_id = resolved_parent.clone();
                    let new_key = match &resolved_parent {
                        Some(eid) => eid.to_string_key(),
                        None => ElementId::root().to_string_key(),
                    };
                    self.pending_inserts.entry(new_key).or_default().push(op);
                }
            }
        }

        // 6. Update vacuumed_intervals
        for tombstone_key in &tombstone_keys {
            if let Some(elem) = self.elements.get(tombstone_key) {
                let rid = elem.id.replica_id.clone();
                let seq = elem.id.sequence;
                let intervals = self.vacuumed_intervals.entry(rid).or_default();
                Self::add_interval(intervals, seq, seq);
            }
        }

        // 7. Merge new redirects into persistent vacuumed_redirects
        for (key, target) in redirects {
            self.vacuumed_redirects.insert(key, target);
        }

        // 8. Enforce exclusivity: remove from known_deletions, then remove elements
        for tombstone_key in &tombstone_keys {
            self.known_deletions.remove(tombstone_key);
            self.elements.remove(tombstone_key);
        }

        // 9. Rebuild order
        self.rebuild_order();
    }

    pub fn vacuum_tombstones(&mut self) {
        self.vacuum_tombstones_bounded(None);
    }

    /// Walk the after_id chain from a tombstone to find its nearest non-tombstone ancestor.
    /// Returns None if the ancestor is root.
    fn resolve_ancestor(
        &self,
        start_key: &str,
        tombstone_keys: &HashSet<String>,
    ) -> Option<ElementId> {
        let mut current_key = start_key.to_string();
        let root_key = ElementId::root().to_string_key();
        let mut visited: HashSet<String> = HashSet::new();

        loop {
            if !visited.insert(current_key.clone()) {
                // Cycle detected — fall back to root
                return Some(ElementId::root());
            }

            let elem = match self.elements.get(&current_key) {
                Some(e) => e,
                None => return Some(ElementId::root()), // Missing element — root fallback
            };

            match &elem.after_id {
                None => return Some(ElementId::root()), // Anchored at root
                Some(parent_id) => {
                    let parent_key = parent_id.to_string_key();
                    if parent_key == root_key {
                        return Some(ElementId::root());
                    }
                    if !tombstone_keys.contains(&parent_key) {
                        // Found a surviving ancestor
                        return Some(parent_id.clone());
                    }
                    // Parent is also a tombstone — continue walking
                    current_key = parent_key;
                }
            }
        }
    }

    pub fn get_state_hash(&self) -> String {
        let text = self.get_text();
        let mut hasher = Sha256::new();
        hasher.update(text.as_bytes());
        format!("{:x}", hasher.finalize())[..16].to_string()
    }

    /// Resolve a redirect key to its terminal target by following the full chain.
    /// Performs path compression: all intermediate entries are updated to point
    /// directly to the terminal target, eliminating future multi-hop traversals.
    fn resolve_redirect_chain(&mut self, start_key: &str) -> Option<ElementId> {
        let root_key = ElementId::root().to_string_key();
        let mut path: Vec<String> = Vec::new();
        let mut current_key = start_key.to_string();
        let mut visited: HashSet<String> = HashSet::new();

        // Walk the chain to find the terminal target
        let terminal = loop {
            if !visited.insert(current_key.clone()) {
                // Cycle detected — resolve to root
                break Some(ElementId::root());
            }
            match self.vacuumed_redirects.get(&current_key) {
                None => {
                    // current_key is not a redirect — it is the terminal
                    // If it's the start key itself, there's no redirect entry
                    if current_key == start_key {
                        // No redirect exists for start_key
                        return None;
                    }
                    // Parse the key back to an ElementId
                    if current_key == root_key {
                        break Some(ElementId::root());
                    }
                    if let Some(colon_pos) = current_key.find(':') {
                        let replica = current_key[..colon_pos].to_string();
                        if let Ok(seq) = current_key[colon_pos + 1..].parse::<i64>() {
                            break Some(ElementId { replica_id: replica, sequence: seq });
                        }
                    }
                    break Some(ElementId::root());
                }
                Some(target) => {
                    path.push(current_key.clone());
                    match target {
                        None => break Some(ElementId::root()),
                        Some(tid) => {
                            let tid_key = tid.to_string_key();
                            if tid_key == root_key {
                                break Some(ElementId::root());
                            }
                            // If the target is not itself a redirect, it's the terminal
                            if !self.vacuumed_redirects.contains_key(&tid_key) {
                                break Some(tid.clone());
                            }
                            current_key = tid_key;
                        }
                    }
                }
            }
        };

        // Path compression: update all intermediate entries to point directly
        // to the terminal target
        for intermediate_key in &path {
            self.vacuumed_redirects.insert(intermediate_key.clone(), terminal.clone());
        }

        terminal
    }

    pub fn merge(&mut self, other: &RGADocument) -> usize {
        let mut applied = 0;

        // 1. Union vacuumed_intervals and collect removal candidates.
        //
        // Invariant (Option A — strict physical exclusivity): after merge
        // completes, no element simultaneously resides in self.elements AND
        // is covered by vacuumed_intervals. Physical removal happens in
        // step 1b below, after vacuumed_redirects are path-compressed.
        let mut removal_keys: Vec<String> = Vec::new();
        for (replica, intervals) in &other.vacuumed_intervals {
            let self_intervals = self.vacuumed_intervals.entry(replica.clone()).or_default();
            for &(s, e) in intervals {
                Self::add_interval(self_intervals, s, e);
            }
            // Collect elements covered by imported intervals for removal,
            // and clear their known_deletions entries (exclusivity).
            for &(s, e) in intervals {
                for seq in s..=e {
                    let key = format!("{}:{}", replica, seq);
                    if self.elements.contains_key(&key) {
                        removal_keys.push(key.clone());
                    }
                    self.known_deletions.remove(&key);
                }
            }
        }

        // 2. Union vacuumed_redirects with full path-compression
        let mut newly_imported_redirect_keys: Vec<String> = Vec::new();
        for (key, target) in &other.vacuumed_redirects {
            if !self.vacuumed_redirects.contains_key(key) {
                newly_imported_redirect_keys.push(key.clone());
            }
            // Insert the raw redirect first
            self.vacuumed_redirects.insert(key.clone(), target.clone());
        }
        // Now path-compress all newly imported redirects so they point to
        // their terminal (non-redirect) target, resolving multi-hop chains.
        for key in &newly_imported_redirect_keys {
            self.resolve_redirect_chain(key);
        }
        // Also re-compress any pre-existing redirects whose targets may now
        // themselves be redirects due to the newly imported entries.
        let existing_keys: Vec<String> = self.vacuumed_redirects.keys().cloned().collect();
        for key in &existing_keys {
            self.resolve_redirect_chain(key);
        }

        // 1b. Physical removal of elements covered by imported vacuumed_intervals.
        //
        // Now that vacuumed_redirects are fully path-compressed (step 2), we
        // can safely rewrite parent pointers and remove dead elements.
        // This enforces the strict physical exclusivity invariant.
        if !removal_keys.is_empty() {
            let removal_set: HashSet<String> = removal_keys.iter().cloned().collect();
            let root_key = ElementId::root().to_string_key();

            // Rewrite parent pointers of surviving elements whose after_id
            // references a vacuumed element, using the path-compressed redirects.
            let surviving_keys: Vec<String> = self.elements.keys()
                .filter(|k| !removal_set.contains(*k) && **k != root_key)
                .cloned()
                .collect();
            for key in surviving_keys {
                if let Some(elem) = self.elements.get_mut(&key) {
                    if let Some(ref after_id) = elem.after_id {
                        let after_key = after_id.to_string_key();
                        if removal_set.contains(&after_key) {
                            if let Some(resolved) = self.vacuumed_redirects.get(&after_key) {
                                elem.after_id = resolved.clone();
                            }
                        }
                    }
                }
            }

            // Physical removal
            for key in &removal_keys {
                self.elements.remove(key);
            }

            self.rebuild_order();
        }

        // 3. Drain pending_inserts stranded under vacuumed parents.
        //    Uses a fixed-point loop: keep draining until no more progress is
        //    made, which handles the case where resolving one stranded op
        //    creates a parent that unblocks another stranded op.
        let mut stranded_applied = false;
        loop {
            // Collect all pending keys that are vacuumed redirects
            let drainable_keys: Vec<String> = self
                .pending_inserts
                .keys()
                .filter(|k| self.vacuumed_redirects.contains_key(*k))
                .cloned()
                .collect();

            if drainable_keys.is_empty() {
                break;
            }

            let mut made_progress = false;
            for vacuumed_key in drainable_keys {
                if let Some(stranded_ops) = self.pending_inserts.remove(&vacuumed_key) {
                    let resolved_parent = self
                        .vacuumed_redirects
                        .get(&vacuumed_key)
                        .cloned()
                        .flatten();
                    for mut op in stranded_ops {
                        op.after_id = resolved_parent.clone();
                        let resolved_key = match &resolved_parent {
                            Some(eid) => eid.to_string_key(),
                            None => ElementId::root().to_string_key(),
                        };
                        let parent_exists = match &resolved_parent {
                            Some(eid) => self.elements.contains_key(&eid.to_string_key()),
                            None => true, // root always exists
                        };
                        if parent_exists {
                            // Parent is present — apply immediately.
                            // push_to_ops=false: the op was already logged when
                            // it was first queued via apply_operation (push_to_ops=true).
                            // defer_rebuild=true: we will rebuild order later.
                            if self.apply_operation_internal(&op, false, true) {
                                applied += 1;
                                made_progress = true;
                                if op.op_type == OperationType::INSERT {
                                    stranded_applied = true;
                                }
                            }
                        } else {
                            // Resolved parent also missing — re-key under the resolved parent
                            self.pending_inserts
                                .entry(resolved_key)
                                .or_default()
                                .push(op);
                        }
                    }
                }
            }

            if !made_progress {
                break;
            }
        }
        if stranded_applied {
            self.rebuild_order();
        }

        // 4. Union known_deletions (filtering against exclusivity)
        for deletion in &other.known_deletions {
            // Check if this deletion is already covered by vacuumed_intervals
            if let Some(colon_pos) = deletion.find(':') {
                let replica = &deletion[..colon_pos];
                if let Ok(seq) = deletion[colon_pos + 1..].parse::<i64>() {
                    if self.is_vacuumed(replica, seq) {
                        continue; // Already vacuumed, skip
                    }
                }
            }
            if self.known_deletions.insert(deletion.clone()) {
                applied += 1;
            }
        }

        // 5. Replay operations (checks vacuumed_intervals + redirects)
        applied += self.apply_operations(&other.operations);
        applied
    }

    pub fn translate_char_offset(&self, mut char_offset: usize) -> Option<(ElementId, usize)> {
        for eid in &self.order {
            if let Some(elem) = self.elements.get(&eid.to_string_key()) {
                if !elem.deleted {
                    let len = elem.value.chars().count();
                    if char_offset > 0 && char_offset < len {
                        return Some((eid.clone(), char_offset));
                    }
                    if char_offset < len {
                        break;
                    }
                    char_offset -= len;
                }
            }
        }
        None
    }

    pub fn local_char_insert_split(&mut self, char_offset: usize, insert_str: String) -> Vec<RGAOperation> {
        let mut ops = Vec::new();
        if let Some((target_id, relative_offset)) = self.translate_char_offset(char_offset) {
            // Decompose the token
            let elem = self.elements.get(&target_id.to_string_key()).unwrap().clone();
            let val = elem.value.clone();
            let chars: Vec<char> = val.chars().collect();
            let left: String = chars[..relative_offset].iter().collect();
            let right: String = chars[relative_offset..].iter().collect();

            // Tombstone T
            let del_op = RGAOperation {
                op_type: OperationType::DELETE,
                element_id: target_id.clone(),
                value: None,
                after_id: None,
                timestamp: Self::get_timestamp(),
                deleted_by: None,
            };
            self.apply_operation(&del_op);
            ops.push(del_op);

            // Insert sub-tokens:
            // T1 (left) anchored after T's original after_id
            let id_left = self.next_id();
            let ins_left = RGAOperation {
                op_type: OperationType::INSERT,
                element_id: id_left.clone(),
                value: Some(left),
                after_id: elem.after_id.clone(),
                timestamp: Self::get_timestamp(),
                deleted_by: None,
            };
            self.apply_operation(&ins_left);
            ops.push(ins_left);

            // T_insert anchored after T1
            let id_ins = self.next_id();
            let ins_mid = RGAOperation {
                op_type: OperationType::INSERT,
                element_id: id_ins.clone(),
                value: Some(insert_str),
                after_id: Some(id_left.clone()),
                timestamp: Self::get_timestamp(),
                deleted_by: None,
            };
            self.apply_operation(&ins_mid);
            ops.push(ins_mid);

            // T2 (right) anchored after T_insert
            let id_right = self.next_id();
            let ins_right = RGAOperation {
                op_type: OperationType::INSERT,
                element_id: id_right,
                value: Some(right),
                after_id: Some(id_ins),
                timestamp: Self::get_timestamp(),
                deleted_by: None,
            };
            self.apply_operation(&ins_right);
            ops.push(ins_right);
        } else {
            // Falls exactly on a boundary or start/end
            let mut preceding_id = ElementId::root();
            let mut current_offset = 0;
            for eid in &self.order {
                if let Some(elem) = self.elements.get(&eid.to_string_key()) {
                    if !elem.deleted {
                        let len = elem.value.chars().count();
                        if current_offset + len >= char_offset {
                            break;
                        }
                        current_offset += len;
                        preceding_id = eid.clone();
                    }
                }
            }

            let new_id = self.next_id();
            let op = RGAOperation {
                op_type: OperationType::INSERT,
                element_id: new_id,
                value: Some(insert_str),
                after_id: Some(preceding_id),
                timestamp: Self::get_timestamp(),
                deleted_by: None,
            };
            self.apply_operation(&op);
            ops.push(op);
        }
        ops
    }

    pub fn local_char_delete_split_bounded(&mut self, char_offset: usize, delete_len: usize, active_dot: Option<&crate::causality::Dot>) -> Vec<RGAOperation> {
        let mut ops = Vec::new();
        if let Some((target_id, relative_offset)) = self.translate_char_offset(char_offset) {
            let elem = self.elements.get(&target_id.to_string_key()).unwrap().clone();
            let val = elem.value.clone();
            let chars: Vec<char> = val.chars().collect();
            let len = chars.len();

            if relative_offset + delete_len <= len {
                let left: String = chars[..relative_offset].iter().collect();
                let right: String = chars[relative_offset + delete_len..].iter().collect();

                // Tombstone T
                let del_op = RGAOperation {
                    op_type: OperationType::DELETE,
                    element_id: target_id.clone(),
                    value: None,
                    after_id: None,
                    timestamp: Self::get_timestamp(),
                    deleted_by: active_dot.cloned(),
                };
                self.apply_operation(&del_op);
                ops.push(del_op);

                let mut prev_id = elem.after_id.clone().unwrap_or_else(ElementId::root);
                if !left.is_empty() {
                    let id_left = self.next_id();
                    let ins_left = RGAOperation {
                        op_type: OperationType::INSERT,
                        element_id: id_left.clone(),
                        value: Some(left),
                        after_id: elem.after_id.clone(),
                        timestamp: Self::get_timestamp(),
                        deleted_by: None,
                    };
                    self.apply_operation(&ins_left);
                    ops.push(ins_left);
                    prev_id = id_left;
                }

                if !right.is_empty() {
                    let id_right = self.next_id();
                    let ins_right = RGAOperation {
                        op_type: OperationType::INSERT,
                        element_id: id_right,
                        value: Some(right),
                        after_id: Some(prev_id),
                        timestamp: Self::get_timestamp(),
                        deleted_by: None,
                    };
                    self.apply_operation(&ins_right);
                    ops.push(ins_right);
                }
            }
        } else {
            let mut target_id = None;
            let mut current_offset = 0;
            for eid in &self.order {
                if let Some(elem) = self.elements.get(&eid.to_string_key()) {
                    if !elem.deleted {
                        if current_offset == char_offset {
                            target_id = Some(eid.clone());
                            break;
                        }
                        current_offset += elem.value.chars().count();
                    }
                }
            }

            if let Some(tid) = target_id {
                let op = RGAOperation {
                    op_type: OperationType::DELETE,
                    element_id: tid,
                    value: None,
                    after_id: None,
                    timestamp: Self::get_timestamp(),
                    deleted_by: active_dot.cloned(),
                };
                self.apply_operation(&op);
                ops.push(op);
            }
        }
        ops
    }

    pub fn local_char_delete_split(&mut self, char_offset: usize, delete_len: usize) -> Vec<RGAOperation> {
        self.local_char_delete_split_bounded(char_offset, delete_len, None)
    }
}
