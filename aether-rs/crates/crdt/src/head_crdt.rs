use crate::causality::{Dot, VersionVector};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

/// Default maximum number of concurrent (non-dominated) events allowed in the frontier.
pub const DEFAULT_MAX_FRONTIER_WIDTH: usize = 32;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HeadFrontierError {
    DuplicateDotMismatch(Dot),
    SelfInvalidCausality(Dot),
    FrontierWidthExceeded { current: usize, max: usize },
}

/// A single promotion event in the repository's history.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct HeadEvent {
    pub snapshot_id: String,
    pub dot: Dot,
    pub context: VersionVector,
    pub agent_id: String,
    /// Frozen trust weight at the time of promotion, used strictly for read-time arbitration.
    pub frozen_trust: f64,
}

/// The repository head, modeled as a causal frontier with dominance pruning
/// using Dotted Version Vectors for causality.
#[derive(Debug, Clone, PartialEq, Default, Serialize, Deserialize)]
pub struct HeadFrontier {
    #[serde(serialize_with = "serialize_events", deserialize_with = "deserialize_events")]
    pub events: BTreeMap<Dot, HeadEvent>,
}

fn serialize_events<S>(
    events: &BTreeMap<Dot, HeadEvent>,
    serializer: S,
) -> Result<S::Ok, S::Error>
where
    S: serde::Serializer,
{
    let vec: Vec<&HeadEvent> = events.values().collect();
    vec.serialize(serializer)
}

fn deserialize_events<'de, D>(
    deserializer: D,
) -> Result<BTreeMap<Dot, HeadEvent>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    let vec = Vec::<HeadEvent>::deserialize(deserializer)?;
    let mut map = BTreeMap::new();
    for event in vec {
        map.insert(event.dot.clone(), event);
    }
    Ok(map)
}

impl HeadFrontier {
    pub fn new() -> Self {
        Self { events: BTreeMap::new() }
    }

    /// Safely appends an event locally with the default width cap (`DEFAULT_MAX_FRONTIER_WIDTH`).
    /// Rejects self-invalid causal structures, duplicate-dot payload mismatches,
    /// and appends that would push the frontier beyond the width limit.
    pub fn try_append(&mut self, event: HeadEvent) -> Result<(), HeadFrontierError> {
        self.try_append_checked(event, DEFAULT_MAX_FRONTIER_WIDTH)
    }

    /// Safely appends an event locally with an explicit width cap.
    ///
    /// Validation order:
    /// 1. Reject self-invalid causality (dot ∈ own context).
    /// 2. Reject duplicate-dot payload mismatches.
    /// 3. Simulate dominance pruning and reject if the *post-pruning* frontier
    ///    width would exceed `max_width`.
    ///
    /// This width cap is enforced at the CRDT layer. End-to-end enforcement by
    /// the server promotion path (`crdt_promotion.rs`) is wired to this API.
    pub fn try_append_checked(
        &mut self,
        event: HeadEvent,
        max_width: usize,
    ) -> Result<(), HeadFrontierError> {
        if event.context.includes(&event.dot) {
            return Err(HeadFrontierError::SelfInvalidCausality(event.dot.clone()));
        }

        if let Some(existing) = self.events.get(&event.dot) {
            if existing != &event {
                return Err(HeadFrontierError::DuplicateDotMismatch(event.dot.clone()));
            }
        }

        // Simulate dominance pruning to compute post-append width.
        let surviving = self
            .events
            .keys()
            .filter(|dot| !event.context.includes(dot))
            .count();
        // +1 for the new event itself.
        let post_width = surviving + 1;

        if post_width > max_width {
            return Err(HeadFrontierError::FrontierWidthExceeded {
                current: post_width,
                max: max_width,
            });
        }

        self.append(event);
        Ok(())
    }

    /// Unchecked append: bypasses width validation and causality checks.
    ///
    /// # Warning
    /// This method does NOT enforce the frontier width cap. Callers should
    /// migrate to `try_append()` or `try_append_checked()` for width-safe
    /// promotion. The server promotion path (`crdt_promotion.rs`) is wired
    /// to the checked API.
    pub fn append(&mut self, event: HeadEvent) {
        // Dominance pruning for existing events
        self.events.retain(|dot, _| !event.context.includes(dot));
        self.events.insert(event.dot.clone(), event);
    }

    /// Safely joins another frontier. Validates that the union of the two frontiers
    /// contains no duplicate dots with differing payloads, and no self-invalid events.
    pub fn try_join(&mut self, other: &HeadFrontier) -> Result<(), HeadFrontierError> {
        // Validate self
        for (dot, event) in &self.events {
            if event.context.includes(dot) {
                return Err(HeadFrontierError::SelfInvalidCausality(dot.clone()));
            }
        }

        // Validate other
        for (dot, event) in &other.events {
            if event.context.includes(dot) {
                return Err(HeadFrontierError::SelfInvalidCausality(dot.clone()));
            }
            if let Some(existing) = self.events.get(dot) {
                if existing != event {
                    return Err(HeadFrontierError::DuplicateDotMismatch(dot.clone()));
                }
            }
        }

        self.join(other);
        Ok(())
    }

    /// Merges another HeadFrontier into this one (the pure CRDT join operation).
    /// Follows the properties of Idempotence, Commutativity, and Associativity.
    /// Panics or misbehaves if called with invalid/corrupt frontiers. Use `try_join` for safe boundaries.
    pub fn join(&mut self, other: &HeadFrontier) {
        // 1. Union the sets
        let mut union = self.events.clone();
        for (dot, event) in &other.events {
            union.insert(dot.clone(), event.clone());
        }

        // 2. Dominance Pruning
        // An event E is pruned if there exists another event D in the union such that D.context includes E.dot.
        let mut pruned = BTreeMap::new();
        for (dot, e) in &union {
            let is_dominated = union.iter().any(|(d, _)| d != dot && union.get(d).unwrap().context.includes(dot));
            if !is_dominated {
                pruned.insert(dot.clone(), e.clone());
            }
        }

        self.events = pruned;
    }

    /// Read-time Projection (Arbitration)
    /// Deterministically picks a "winning" snapshot_id when the frontier is logically forked (width > 1).
    /// This DOES NOT converge or mutate the frontier.
    pub fn arbitrate_view(&self) -> Option<String> {
        if self.events.is_empty() {
            return None;
        }
        
        let mut sorted_events: Vec<_> = self.events.values().collect();
        // Arbitration rule: Highest frozen trust wins. Break ties by lexicographical agent_id.
        sorted_events.sort_by(|a, b| {
            b.frozen_trust
                .total_cmp(&a.frozen_trust)
                .then_with(|| a.agent_id.cmp(&b.agent_id))
        });
        
        Some(sorted_events[0].snapshot_id.clone())
    }

    /// Returns a merged VersionVector summarizing all events in the active frontier.
    /// Used by agents to generate the `context` for their next promotion.
    pub fn causal_context(&self) -> VersionVector {
        let mut ctx = VersionVector::new();
        for e in self.events.values() {
            // Merge the context of the event
            ctx.merge(&e.context);
            // And include the event itself!
            let dot_ctx = {
                let mut c = VersionVector::new();
                c.versions.insert(e.dot.node_id.clone(), e.dot.sequence);
                c
            };
            ctx.merge(&dot_ctx);
        }
        ctx
    }

    /// Returns the component-wise minimum version vector across all events in the
    /// active frontier. This represents the causal floor: the set of events that
    /// every concurrent branch has definitely observed.
    ///
    /// For a single-branch frontier (width == 1), this equals `causal_context()`.
    /// For a forked frontier (width > 1), this is the intersection/meet of all
    /// branches' causal contexts — the safe threshold for GC/vacuuming, since any
    /// element covered by the meet has been observed by all branches.
    ///
    /// Returns an empty VersionVector if the frontier is empty.
    pub fn meet_frontier(&self) -> VersionVector {
        if self.events.is_empty() {
            return VersionVector::new();
        }

        let mut iter = self.events.values();

        // Initialize with the first event's full causal context (context + dot itself)
        let first = iter.next().unwrap();
        let mut meet = first.context.clone();
        meet.versions
            .entry(first.dot.node_id.clone())
            .and_modify(|s| *s = (*s).max(first.dot.sequence))
            .or_insert(first.dot.sequence);

        // Intersect with each subsequent event's full causal context
        for event in iter {
            let mut event_ctx = event.context.clone();
            event_ctx
                .versions
                .entry(event.dot.node_id.clone())
                .and_modify(|s| *s = (*s).max(event.dot.sequence))
                .or_insert(event.dot.sequence);

            // Component-wise minimum: keep only keys present in both, take min
            let mut new_meet = VersionVector::new();
            for (node_id, &meet_seq) in &meet.versions {
                if let Some(&event_seq) = event_ctx.versions.get(node_id) {
                    new_meet
                        .versions
                        .insert(node_id.clone(), meet_seq.min(event_seq));
                }
                // If the key is missing from event_ctx, it's dropped (min with 0 = 0)
            }
            meet = new_meet;
        }

        meet
    }
}
