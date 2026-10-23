use crdt::causality::{Dot, VersionVector};
use crdt::head_crdt::{HeadEvent, HeadFrontier, HeadFrontierError, DEFAULT_MAX_FRONTIER_WIDTH};
use proptest::prelude::*;

// Strategy to generate a valid HeadEvent
fn arb_valid_event() -> impl Strategy<Value = HeadEvent> {
    (
        "[a-z]{3,5}_agent", // agent_id
        1..100u64, // sequence
        0.0..1.0f64, // frozen_trust
        0..3usize // number of dependencies in VersionVector
    ).prop_flat_map(|(agent, seq, trust, num_deps)| {
        let agent_clone = agent.clone();
        proptest::collection::hash_map("[a-z]{3}_dep", 1..50u64, num_deps)
            .prop_map(move |deps| {
                let mut ctx = VersionVector::new();
                for (d, s) in deps {
                    // Ensure self-invalid causality is avoided
                    if d != agent_clone {
                        ctx.versions.insert(d, s);
                    }
                }
                HeadEvent {
                    snapshot_id: format!("snap_{}_{}", agent_clone, seq),
                    dot: Dot::new(agent_clone.clone(), seq),
                    context: ctx,
                    agent_id: agent_clone.clone(),
                    frozen_trust: trust,
                }
            })
    })
}

fn arb_frontier() -> impl Strategy<Value = HeadFrontier> {
    proptest::collection::vec(arb_valid_event(), 0..5).prop_map(|events| {
        let mut f = HeadFrontier::new();
        for e in events {
            let _ = f.try_append(e); // Build naturally valid frontier
        }
        f
    })
}

proptest! {
    #[test]
    fn test_idempotence(f in arb_frontier()) {
        let mut f2 = f.clone();
        f2.join(&f);
        assert_eq!(f, f2);
    }

    #[test]
    fn test_commutativity(f1 in arb_frontier(), f2 in arb_frontier()) {
        let mut a_join_b = f1.clone();
        a_join_b.join(&f2);

        let mut b_join_a = f2.clone();
        b_join_a.join(&f1);

        assert_eq!(a_join_b, b_join_a);
    }

    #[test]
    fn test_associativity(a in arb_frontier(), b in arb_frontier(), c in arb_frontier()) {
        let mut bc = b.clone();
        bc.join(&c);
        let mut a_bc = a.clone();
        a_bc.join(&bc);

        let mut ab = a.clone();
        ab.join(&b);
        let mut ab_c = ab.clone();
        ab_c.join(&c);

        assert_eq!(a_bc, ab_c);
    }
}

// ── Invalid State Rejection Suite ──────────────────────────────────────────

#[test]
fn test_rejects_self_invalid_causality() {
    let mut ctx = VersionVector::new();
    ctx.versions.insert("agent_a".to_string(), 5);
    
    let event = HeadEvent {
        snapshot_id: "snap".to_string(),
        dot: Dot::new("agent_a", 5), // self-invalid!
        context: ctx,
        agent_id: "agent_a".to_string(),
        frozen_trust: 1.0,
    };
    
    let mut f = HeadFrontier::new();
    assert_eq!(
        f.try_append(event),
        Err(HeadFrontierError::SelfInvalidCausality(Dot::new("agent_a", 5)))
    );
}

#[test]
fn test_rejects_duplicate_dot_mismatch() {
    let ev1 = HeadEvent {
        snapshot_id: "snap_1".to_string(),
        dot: Dot::new("agent_a", 1),
        context: VersionVector::new(),
        agent_id: "agent_a".to_string(),
        frozen_trust: 1.0,
    };
    
    let ev2_mismatch = HeadEvent {
        snapshot_id: "snap_2".to_string(), // mismatched payload
        dot: Dot::new("agent_a", 1),
        context: VersionVector::new(),
        agent_id: "agent_a".to_string(),
        frozen_trust: 1.0,
    };
    
    let mut f1 = HeadFrontier::new();
    f1.try_append(ev1).unwrap();
    
    assert_eq!(
        f1.try_append(ev2_mismatch.clone()),
        Err(HeadFrontierError::DuplicateDotMismatch(Dot::new("agent_a", 1)))
    );
    
    let mut f2 = HeadFrontier::new();
    f2.try_append(ev2_mismatch).unwrap();
    
    assert_eq!(
        f1.try_join(&f2),
        Err(HeadFrontierError::DuplicateDotMismatch(Dot::new("agent_a", 1)))
    );
}

// ── Meet Frontier Tests ────────────────────────────────────────────────────

#[test]
fn test_meet_frontier_empty() {
    let f = HeadFrontier::new();
    let meet = f.meet_frontier();
    assert!(meet.versions.is_empty());
}

#[test]
fn test_meet_frontier_single_branch() {
    // Single branch: meet == causal_context
    let mut f = HeadFrontier::new();
    let mut ctx = VersionVector::new();
    ctx.versions.insert("agent_a".to_string(), 3);
    ctx.versions.insert("agent_b".to_string(), 2);

    f.try_append(HeadEvent {
        snapshot_id: "snap_c_1".to_string(),
        dot: Dot::new("agent_c", 1),
        context: ctx,
        agent_id: "agent_c".to_string(),
        frozen_trust: 1.0,
    })
    .unwrap();

    let meet = f.meet_frontier();
    let ctx = f.causal_context();

    // For a single event, meet should equal causal_context
    assert_eq!(meet.versions, ctx.versions);
}

#[test]
fn test_meet_frontier_forked() {
    // Forked frontier with two concurrent branches
    let mut f = HeadFrontier::new();

    // Branch 1: agent_a saw agent_b up to seq 5, agent_c up to seq 3
    let mut ctx1 = VersionVector::new();
    ctx1.versions.insert("agent_b".to_string(), 5);
    ctx1.versions.insert("agent_c".to_string(), 3);
    f.try_append(HeadEvent {
        snapshot_id: "snap_a_1".to_string(),
        dot: Dot::new("agent_a", 1),
        context: ctx1,
        agent_id: "agent_a".to_string(),
        frozen_trust: 1.0,
    })
    .unwrap();

    // Branch 2: agent_d saw agent_b up to seq 2, agent_c up to seq 7
    let mut ctx2 = VersionVector::new();
    ctx2.versions.insert("agent_b".to_string(), 2);
    ctx2.versions.insert("agent_c".to_string(), 7);
    f.try_append(HeadEvent {
        snapshot_id: "snap_d_1".to_string(),
        dot: Dot::new("agent_d", 1),
        context: ctx2,
        agent_id: "agent_d".to_string(),
        frozen_trust: 0.9,
    })
    .unwrap();

    // Both branches should be in the frontier (concurrent, neither dominates)
    assert_eq!(f.events.len(), 2);

    let meet = f.meet_frontier();

    // Meet should be the component-wise minimum:
    // agent_a: only in branch 1 (as dot) → dropped
    // agent_b: min(5, 2) = 2
    // agent_c: min(3, 7) = 3
    // agent_d: only in branch 2 (as dot) → dropped
    assert_eq!(meet.versions.get("agent_b"), Some(&2));
    assert_eq!(meet.versions.get("agent_c"), Some(&3));
    assert_eq!(meet.versions.get("agent_a"), None, "agent_a only in branch 1 dot, not in branch 2");
    assert_eq!(meet.versions.get("agent_d"), None, "agent_d only in branch 2 dot, not in branch 1");
}

// ── Frontier Width Cap Tests ───────────────────────────────────────────────

#[test]
fn test_width_cap_default_constant() {
    assert_eq!(DEFAULT_MAX_FRONTIER_WIDTH, 32);
}

/// Build a frontier of width == max, then try to append a concurrent (non-dominating) event.
/// Must be rejected with FrontierWidthExceeded.
#[test]
fn test_width_cap_rejects_over_limit() {
    let max = 4;
    let mut f = HeadFrontier::new();

    // Fill frontier to max width with concurrent (non-dominating) events.
    for i in 0..max {
        let agent = format!("agent_{}", i);
        let event = HeadEvent {
            snapshot_id: format!("snap_{}", i),
            dot: Dot::new(&agent, 1),
            context: VersionVector::new(),
            agent_id: agent,
            frozen_trust: 1.0,
        };
        f.try_append_checked(event, max).unwrap();
    }
    assert_eq!(f.events.len(), max);

    // One more concurrent event should be rejected.
    let over = HeadEvent {
        snapshot_id: "snap_over".to_string(),
        dot: Dot::new("agent_over", 1),
        context: VersionVector::new(),
        agent_id: "agent_over".to_string(),
        frozen_trust: 1.0,
    };
    assert_eq!(
        f.try_append_checked(over, max),
        Err(HeadFrontierError::FrontierWidthExceeded {
            current: max + 1,
            max,
        })
    );
}

/// Build a frontier of width == max-1, then append a concurrent event. Should succeed.
#[test]
fn test_width_cap_allows_at_limit() {
    let max = 4;
    let mut f = HeadFrontier::new();

    // Fill to max - 1.
    for i in 0..(max - 1) {
        let agent = format!("agent_{}", i);
        let event = HeadEvent {
            snapshot_id: format!("snap_{}", i),
            dot: Dot::new(&agent, 1),
            context: VersionVector::new(),
            agent_id: agent,
            frozen_trust: 1.0,
        };
        f.try_append_checked(event, max).unwrap();
    }
    assert_eq!(f.events.len(), max - 1);

    // One more concurrent event should bring us exactly to max — allowed.
    let at_limit = HeadEvent {
        snapshot_id: "snap_at_limit".to_string(),
        dot: Dot::new("agent_at_limit", 1),
        context: VersionVector::new(),
        agent_id: "agent_at_limit".to_string(),
        frozen_trust: 1.0,
    };
    assert!(f.try_append_checked(at_limit, max).is_ok());
    assert_eq!(f.events.len(), max);
}

/// Build a frontier of width == max, then append an event that dominates all existing
/// events (its context covers every existing dot). After dominance pruning the width
/// drops to 1, which is within the cap. Must succeed.
#[test]
fn test_width_cap_dominance_pruning_allows_append() {
    let max = 4;
    let mut f = HeadFrontier::new();

    // Fill frontier to max width.
    for i in 0..max {
        let agent = format!("agent_{}", i);
        let event = HeadEvent {
            snapshot_id: format!("snap_{}", i),
            dot: Dot::new(&agent, 1),
            context: VersionVector::new(),
            agent_id: agent,
            frozen_trust: 1.0,
        };
        f.try_append_checked(event, max).unwrap();
    }
    assert_eq!(f.events.len(), max);

    // Build a context that dominates all existing events.
    let mut dominating_ctx = VersionVector::new();
    for i in 0..max {
        dominating_ctx
            .versions
            .insert(format!("agent_{}", i), 1);
    }

    let dominator = HeadEvent {
        snapshot_id: "snap_dominator".to_string(),
        dot: Dot::new("agent_dominator", 1),
        context: dominating_ctx,
        agent_id: "agent_dominator".to_string(),
        frozen_trust: 1.0,
    };

    // Even though current width == max, dominance pruning reduces it to 1 — allowed.
    assert!(f.try_append_checked(dominator, max).is_ok());
    assert_eq!(f.events.len(), 1);
}

#[test]
fn test_frontier_different_insertion_order_cas_equal() {
    let mut vv1 = VersionVector::new();
    vv1.versions.insert("agent_a".to_string(), 10);
    vv1.versions.insert("agent_b".to_string(), 20);
    vv1.versions.insert("agent_c".to_string(), 30);

    let mut vv2 = VersionVector::new();
    vv2.versions.insert("agent_c".to_string(), 30);
    vv2.versions.insert("agent_b".to_string(), 20);
    vv2.versions.insert("agent_a".to_string(), 10);

    let ev1 = HeadEvent {
        snapshot_id: "snap_1".to_string(),
        dot: Dot::new("agent_x", 1),
        context: vv1,
        agent_id: "agent_x".to_string(),
        frozen_trust: 1.0,
    };

    let ev2 = HeadEvent {
        snapshot_id: "snap_1".to_string(),
        dot: Dot::new("agent_x", 1),
        context: vv2,
        agent_id: "agent_x".to_string(),
        frozen_trust: 1.0,
    };

    let mut f1 = HeadFrontier::new();
    f1.try_append(ev1).unwrap();

    let mut f2 = HeadFrontier::new();
    f2.try_append(ev2).unwrap();

    let json1 = serde_json::to_string(&f1).unwrap();
    let json2 = serde_json::to_string(&f2).unwrap();
    
    assert_eq!(json1, json2, "Serialized JSONs must be identical regardless of insertion order");
}
