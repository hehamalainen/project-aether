use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

/// A Dot represents a single event (e.g., a promotion) created by a specific node.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct Dot {
    pub node_id: String,
    pub sequence: u64,
}

impl Dot {
    pub fn new(node_id: impl Into<String>, sequence: u64) -> Self {
        Self {
            node_id: node_id.into(),
            sequence,
        }
    }
}

/// A VersionVector maps Node IDs to the highest sequence number observed from that node.
/// It compactly represents the causal history (the context) of a state.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct VersionVector {
    pub versions: BTreeMap<String, u64>,
}

impl VersionVector {
    pub fn new() -> Self {
        Self {
            versions: BTreeMap::new(),
        }
    }

    /// Increment the sequence number for a given node and return the resulting Dot.
    pub fn increment(&mut self, node_id: &str) -> Dot {
        let seq = self.versions.entry(node_id.to_string()).or_insert(0);
        *seq += 1;
        Dot::new(node_id, *seq)
    }

    /// Check if this VersionVector includes (has observed) the given Dot.
    pub fn includes(&self, dot: &Dot) -> bool {
        match self.versions.get(&dot.node_id) {
            Some(&seq) => seq >= dot.sequence,
            None => false,
        }
    }

    /// Merge another VersionVector into this one, taking the element-wise maximum.
    pub fn merge(&mut self, other: &VersionVector) {
        for (node_id, &seq) in &other.versions {
            let entry = self.versions.entry(node_id.clone()).or_insert(0);
            *entry = std::cmp::max(*entry, seq);
        }
    }

    /// True if this VersionVector has observed all events represented by `other`.
    pub fn dominates(&self, other: &VersionVector) -> bool {
        for (node_id, &seq) in &other.versions {
            if !self.includes(&Dot::new(node_id, seq)) {
                return false;
            }
        }
        true
    }
}
