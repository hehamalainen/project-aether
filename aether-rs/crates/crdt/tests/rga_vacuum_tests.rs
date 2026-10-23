use crdt::rga::{ElementId, OperationType, RGADocument, RGAOperation};

fn make_insert(
    replica_id: &str,
    sequence: i64,
    value: &str,
    after_id: Option<ElementId>,
) -> RGAOperation {
    RGAOperation {
        op_type: OperationType::INSERT,
        element_id: ElementId {
            replica_id: replica_id.to_string(),
            sequence,
        },
        value: Some(value.to_string()),
        after_id,
        timestamp: sequence as f64,
        deleted_by: None,
    }
}

fn make_delete(replica_id: &str, sequence: i64) -> RGAOperation {
    RGAOperation {
        op_type: OperationType::DELETE,
        element_id: ElementId {
            replica_id: replica_id.to_string(),
            sequence,
        },
        value: None,
        after_id: None,
        timestamp: sequence as f64 + 0.5,
        deleted_by: None,
    }
}

fn eid(replica: &str, seq: i64) -> ElementId {
    ElementId {
        replica_id: replica.to_string(),
        sequence: seq,
    }
}

#[test]
fn test_vacuum_tombstones_elements_order() {
    let mut doc = RGADocument::new("actor_a".to_string());

    // Insert: root <- A <- B <- C
    doc.apply_operation(&make_insert("actor_a", 1, "A", Some(ElementId::root())));
    doc.apply_operation(&make_insert("actor_a", 2, "B", Some(eid("actor_a", 1))));
    doc.apply_operation(&make_insert("actor_a", 3, "C", Some(eid("actor_a", 2))));
    assert_eq!(doc.get_text(), "ABC");

    // Delete B
    doc.apply_operation(&make_delete("actor_a", 2));
    assert_eq!(doc.get_text(), "AC");

    // Vacuum
    doc.vacuum_tombstones();
    assert_eq!(doc.get_text(), "AC");

    // No deleted elements remain (except root which is never deleted)
    for (key, elem) in &doc.elements {
        assert!(
            !elem.deleted,
            "Element {} should not be deleted after vacuum",
            key
        );
    }

    // B is gone from elements
    assert!(!doc.elements.contains_key(&eid("actor_a", 2).to_string_key()));

    // vacuumed_intervals contains B
    assert!(doc.is_vacuumed("actor_a", 2));

    // C's after_id should now be A (since B was vacuumed and B's parent was A)
    let c_elem = doc.elements.get(&eid("actor_a", 3).to_string_key()).unwrap();
    assert_eq!(c_elem.after_id, Some(eid("actor_a", 1)));
}

#[test]
fn test_vacuum_tombstones_transitive() {
    // Chain: root <- A1 <- B1 <- A2
    // Delete B1 and A2 (not A1)
    let mut doc = RGADocument::new("actor_a".to_string());

    doc.apply_operation(&make_insert("actor_a", 1, "A1", Some(ElementId::root())));
    doc.apply_operation(&make_insert("actor_b", 1, "B1", Some(eid("actor_a", 1))));
    doc.apply_operation(&make_insert("actor_a", 2, "A2", Some(eid("actor_b", 1))));

    // Insert child X after A2
    doc.apply_operation(&make_insert("actor_c", 1, "X", Some(eid("actor_a", 2))));
    assert_eq!(doc.get_text(), "A1B1A2X");

    // Delete B1 and A2
    doc.apply_operation(&make_delete("actor_b", 1));
    doc.apply_operation(&make_delete("actor_a", 2));
    assert_eq!(doc.get_text(), "A1X");

    doc.vacuum_tombstones();
    assert_eq!(doc.get_text(), "A1X");

    // X's after_id should now be A1 (walking: X -> A2 [tombstone] -> B1 [tombstone] -> A1 [alive])
    let x_elem = doc.elements.get(&eid("actor_c", 1).to_string_key()).unwrap();
    assert_eq!(x_elem.after_id, Some(eid("actor_a", 1)));

    // Both B1 and A2 are in vacuumed_intervals
    assert!(doc.is_vacuumed("actor_b", 1));
    assert!(doc.is_vacuumed("actor_a", 2));
}

#[test]
fn test_vacuum_tombstones_cross_replica_parent() {
    // Chain: root <- A1 <- B1 <- A2, child X anchored after A2
    // Delete only A2 — the correct surviving ancestor is B1 (from replica B, not A1)
    let mut doc = RGADocument::new("actor_a".to_string());

    doc.apply_operation(&make_insert("actor_a", 1, "A1", Some(ElementId::root())));
    doc.apply_operation(&make_insert("actor_b", 1, "B1", Some(eid("actor_a", 1))));
    doc.apply_operation(&make_insert("actor_a", 2, "A2", Some(eid("actor_b", 1))));
    doc.apply_operation(&make_insert("actor_c", 1, "X", Some(eid("actor_a", 2))));
    assert_eq!(doc.get_text(), "A1B1A2X");

    // Delete only A2
    doc.apply_operation(&make_delete("actor_a", 2));
    assert_eq!(doc.get_text(), "A1B1X");

    doc.vacuum_tombstones();
    assert_eq!(doc.get_text(), "A1B1X");

    // X's after_id must be B1 (cross-replica), NOT A1
    let x_elem = doc.elements.get(&eid("actor_c", 1).to_string_key()).unwrap();
    assert_eq!(
        x_elem.after_id,
        Some(eid("actor_b", 1)),
        "X should be anchored to B1 (cross-replica parent), not A1"
    );

    // Redirect should record the mapping
    assert_eq!(
        doc.vacuumed_redirects.get(&eid("actor_a", 2).to_string_key()),
        Some(&Some(eid("actor_b", 1)))
    );
}

#[test]
fn test_vacuum_tombstones_operations_log() {
    let mut doc = RGADocument::new("actor_a".to_string());

    doc.apply_operation(&make_insert("actor_a", 1, "A", Some(ElementId::root())));
    doc.apply_operation(&make_insert("actor_a", 2, "B", Some(eid("actor_a", 1))));
    doc.apply_operation(&make_insert("actor_a", 3, "C", Some(eid("actor_a", 2))));
    doc.apply_operation(&make_delete("actor_a", 2));

    let ops_before = doc.operations.len();
    assert!(ops_before >= 4); // At least 3 inserts + 1 delete

    doc.vacuum_tombstones();

    // Operations for vacuumed element B (actor_a:2) should be pruned
    for op in &doc.operations {
        assert_ne!(
            op.element_id,
            eid("actor_a", 2),
            "Operations for vacuumed element should be pruned"
        );
    }

    // Surviving ops with after_id pointing to B should be rewritten to A
    for op in &doc.operations {
        if let Some(ref after_id) = op.after_id {
            assert_ne!(
                *after_id,
                eid("actor_a", 2),
                "after_id should not reference vacuumed element"
            );
        }
    }

    // C's insert op should now have after_id = A1
    let c_op = doc
        .operations
        .iter()
        .find(|op| op.element_id == eid("actor_a", 3))
        .expect("C's insert op should survive");
    assert_eq!(c_op.after_id, Some(eid("actor_a", 1)));
}

#[test]
fn test_vacuum_anti_resurrection_merge() {
    // 1. replica_a inserts X (actor_a, seq 1)
    let mut replica_a = RGADocument::new("actor_a".to_string());
    replica_a.apply_operation(&make_insert("actor_a", 1, "X", Some(ElementId::root())));
    assert_eq!(replica_a.get_text(), "X");

    // 2. replica_b receives and applies only INSERT X. B has X active.
    let mut replica_b = RGADocument::new("actor_b".to_string());
    replica_b.apply_operation(&make_insert("actor_a", 1, "X", Some(ElementId::root())));
    assert_eq!(replica_b.get_text(), "X");

    // 3. replica_a deletes X, then vacuums
    replica_a.apply_operation(&make_delete("actor_a", 1));
    replica_a.vacuum_tombstones();
    assert_eq!(replica_a.get_text(), "");
    assert!(!replica_a.elements.contains_key(&eid("actor_a", 1).to_string_key()));
    assert!(replica_a.is_vacuumed("actor_a", 1));
    assert_eq!(
        replica_a.vacuumed_redirects.get(&eid("actor_a", 1).to_string_key()),
        Some(&Some(ElementId::root()))
    );

    // 4. replica_a.merge(replica_b) — B sends INSERT X.
    //    A must discard the INSERT because seq 1 is in vacuumed interval.
    replica_a.merge(&replica_b);
    assert_eq!(replica_a.get_text(), "");

    // 5. replica_b.merge(replica_a) — B receives A's vacuumed_intervals and vacuumed_redirects.
    //    B must mark X deleted and union the interval.
    replica_b.merge(&replica_a);
    assert!(replica_b.is_vacuumed("actor_a", 1));

    // 6. Both converge to empty text, X stays deleted.
    assert_eq!(replica_a.get_text(), "");
    assert_eq!(replica_b.get_text(), "");
    assert_eq!(replica_a.get_state_hash(), replica_b.get_state_hash());
}

#[test]
fn test_vacuum_stale_insert_with_vacuumed_parent() {
    // 1. replica_a inserts A1, then B1 (from replica_b) after A1, then A2 after B1
    let mut replica_a = RGADocument::new("actor_a".to_string());
    replica_a.apply_operation(&make_insert("actor_a", 1, "A1", Some(ElementId::root())));
    replica_a.apply_operation(&make_insert("actor_b", 1, "B1", Some(eid("actor_a", 1))));
    replica_a.apply_operation(&make_insert("actor_a", 2, "A2", Some(eid("actor_b", 1))));
    assert_eq!(replica_a.get_text(), "A1B1A2");

    // 2. replica_a deletes A2, vacuums
    replica_a.apply_operation(&make_delete("actor_a", 2));
    replica_a.vacuum_tombstones();
    assert_eq!(replica_a.get_text(), "A1B1");

    // Assert redirect
    assert_eq!(
        replica_a.vacuumed_redirects.get(&eid("actor_a", 2).to_string_key()),
        Some(&Some(eid("actor_b", 1))),
        "A2 should redirect to B1 (cross-replica)"
    );

    // 3. A stale replica_c arrives with an INSERT of C1 anchored after_id = A2
    let stale_insert = make_insert("actor_c", 1, "C1", Some(eid("actor_a", 2)));
    replica_a.apply_operation(&stale_insert);

    // Assert: C1 is applied with rewritten after_id = B1
    assert!(
        replica_a.elements.contains_key(&eid("actor_c", 1).to_string_key()),
        "C1 should be applied"
    );
    let c1_elem = replica_a
        .elements
        .get(&eid("actor_c", 1).to_string_key())
        .unwrap();
    assert_eq!(
        c1_elem.after_id,
        Some(eid("actor_b", 1)),
        "C1 should be anchored to B1 after redirect"
    );

    // 4. Independently, replica_b (which also has the vacuum state) applies the same stale INSERT
    let mut replica_b = RGADocument::new("actor_b".to_string());
    replica_b.apply_operation(&make_insert("actor_a", 1, "A1", Some(ElementId::root())));
    replica_b.apply_operation(&make_insert("actor_b", 1, "B1", Some(eid("actor_a", 1))));
    replica_b.apply_operation(&make_insert("actor_a", 2, "A2", Some(eid("actor_b", 1))));
    replica_b.apply_operation(&make_delete("actor_a", 2));
    replica_b.vacuum_tombstones();

    replica_b.apply_operation(&stale_insert);

    // Assert: Same rewrite to B1, same final document state
    assert_eq!(
        replica_a.get_text(),
        replica_b.get_text(),
        "Both replicas must converge after applying same stale insert"
    );
    assert_eq!(replica_a.get_state_hash(), replica_b.get_state_hash());
}

#[test]
fn test_vacuum_merge_drains_stranded_pending_inserts() {
    // 1. replica_c receives an INSERT of child X with after_id = A2, but A2 has not arrived yet.
    let mut replica_c = RGADocument::new("actor_c".to_string());

    let stale_insert_x = make_insert("actor_x", 1, "X", Some(eid("actor_a", 2)));
    replica_c.apply_operation(&stale_insert_x);

    // Assert: X is stranded in pending_inserts
    let a2_key = eid("actor_a", 2).to_string_key();
    assert!(
        replica_c.pending_inserts.contains_key(&a2_key),
        "X should be queued under A2"
    );
    assert!(
        !replica_c.elements.contains_key(&eid("actor_x", 1).to_string_key()),
        "X should not be in elements yet"
    );

    // 2. Separately, replica_a builds and vacuums
    let mut replica_a = RGADocument::new("actor_a".to_string());
    replica_a.apply_operation(&make_insert("actor_a", 1, "A1", Some(ElementId::root())));
    replica_a.apply_operation(&make_insert("actor_b", 1, "B1", Some(eid("actor_a", 1))));
    replica_a.apply_operation(&make_insert("actor_a", 2, "A2", Some(eid("actor_b", 1))));
    replica_a.apply_operation(&make_delete("actor_a", 2));
    replica_a.vacuum_tombstones();
    assert_eq!(replica_a.get_text(), "A1B1");

    // 3. replica_c.merge(replica_a) — C imports vacuum state
    replica_c.merge(&replica_a);

    // Assert: pending_inserts no longer has key "actor_a:2"
    assert!(
        !replica_c.pending_inserts.contains_key(&a2_key),
        "Stranded pending_inserts under A2 should be drained after merge"
    );

    // B1 should be present in C's elements (delivered via merge's operations replay)
    let b1_key = eid("actor_b", 1).to_string_key();
    assert!(
        replica_c.elements.contains_key(&b1_key),
        "B1 should be delivered via merge"
    );

    // X should now be applied (B1 is present, so it should be applied immediately)
    let x_key = eid("actor_x", 1).to_string_key();
    assert!(
        replica_c.elements.contains_key(&x_key),
        "X should be applied after pending drain resolved parent B1"
    );

    let x_elem = replica_c.elements.get(&x_key).unwrap();
    assert_eq!(
        x_elem.after_id,
        Some(eid("actor_b", 1)),
        "X should be anchored to B1 (resolved from vacuumed A2)"
    );
    assert!(!x_elem.deleted);

    // 4. Full convergence: merge everything back
    replica_a.merge(&replica_c);
    assert_eq!(
        replica_a.get_text(),
        replica_c.get_text(),
        "Both replicas must converge"
    );
}

#[test]
fn test_vacuum_merge_drains_stranded_pending_rekey() {
    // Variant: the resolved parent is also missing, so the op is re-keyed, not applied.

    // 1. replica_c has child X waiting on A2
    let mut replica_c = RGADocument::new("actor_c".to_string());
    let stale_insert_x = make_insert("actor_x", 1, "X", Some(eid("actor_a", 2)));
    replica_c.apply_operation(&stale_insert_x);

    let a2_key = eid("actor_a", 2).to_string_key();
    assert!(replica_c.pending_inserts.contains_key(&a2_key));

    // 2. Build a replica_a that has vacuumed A2 with redirect A2 -> B1,
    //    but DON'T include B1 in the operations so C won't learn about B1 from the merge.
    //    We simulate this by manually constructing vacuum state.
    let mut replica_a = RGADocument::new("actor_a".to_string());
    // Only insert A1 and A2, delete A2, vacuum — B1 doesn't exist
    replica_a.apply_operation(&make_insert("actor_a", 1, "A1", Some(ElementId::root())));
    replica_a.apply_operation(&make_insert("actor_a", 2, "A2", Some(eid("actor_a", 1))));
    replica_a.apply_operation(&make_delete("actor_a", 2));
    replica_a.vacuum_tombstones();

    // A2's redirect resolves to A1 (since B1 doesn't exist in this replica)
    assert_eq!(
        replica_a.vacuumed_redirects.get(&a2_key),
        Some(&Some(eid("actor_a", 1)))
    );

    // Now manually override the redirect to point to B1 (which C doesn't have)
    // This simulates a scenario where the actual parent chain goes through B1
    replica_a.vacuumed_redirects.insert(
        a2_key.clone(),
        Some(eid("actor_b", 1)),
    );

    // 3. replica_c.merge(replica_a) — C imports vacuum state, but B1 isn't in C's elements
    replica_c.merge(&replica_a);

    // Assert: pending_inserts no longer has key A2
    assert!(
        !replica_c.pending_inserts.contains_key(&a2_key),
        "A2 queue should be drained"
    );

    // X should be re-keyed under B1 (since B1 isn't in elements)
    let b1_key = eid("actor_b", 1).to_string_key();
    assert!(
        replica_c.pending_inserts.contains_key(&b1_key),
        "X should be re-keyed under B1 since B1 is not yet available"
    );

    // X is NOT yet in elements
    assert!(
        !replica_c.elements.contains_key(&eid("actor_x", 1).to_string_key()),
        "X should not be applied yet — B1 is missing"
    );

    // 4. Now deliver B1 — X should drain naturally
    replica_c.apply_operation(&make_insert("actor_b", 1, "B1", Some(eid("actor_a", 1))));
    assert!(
        replica_c.elements.contains_key(&eid("actor_x", 1).to_string_key()),
        "X should be applied after B1 arrives"
    );
}

#[test]
fn test_vacuum_serde_roundtrip() {
    let mut doc = RGADocument::new("actor_a".to_string());
    doc.apply_operation(&make_insert("actor_a", 1, "A", Some(ElementId::root())));
    doc.apply_operation(&make_insert("actor_a", 2, "B", Some(eid("actor_a", 1))));
    doc.apply_operation(&make_delete("actor_a", 2));
    doc.vacuum_tombstones();

    let text_before = doc.get_text();
    let hash_before = doc.get_state_hash();

    let json = serde_json::to_string(&doc).unwrap();
    let restored: RGADocument = serde_json::from_str(&json).unwrap();

    assert_eq!(restored.get_text(), text_before);
    assert_eq!(restored.get_state_hash(), hash_before);
    assert!(restored.is_vacuumed("actor_a", 2));
    assert!(restored.vacuumed_redirects.contains_key(&eid("actor_a", 2).to_string_key()));
}

#[test]
fn test_vacuum_interval_merge_compression() {
    let mut doc = RGADocument::new("actor_a".to_string());

    // Insert elements 1-5
    for i in 1..=5 {
        let after = if i == 1 {
            ElementId::root()
        } else {
            eid("actor_a", i - 1)
        };
        doc.apply_operation(&make_insert("actor_a", i, &format!("{}", i), Some(after)));
    }
    assert_eq!(doc.get_text(), "12345");

    // Delete all 5
    for i in 1..=5 {
        doc.apply_operation(&make_delete("actor_a", i));
    }

    doc.vacuum_tombstones();

    // All 5 should be compressed into a single interval [1, 5]
    let intervals = doc.vacuumed_intervals.get("actor_a").unwrap();
    assert_eq!(intervals.len(), 1, "Should be compressed to single interval");
    assert_eq!(intervals[0], (1, 5));
}

/// Regression test for Bug 1: multi-hop redirect stranding.
///
/// Constructs a redirect chain D→C→B→A (3 hops deep) across two replicas.
/// A child op X is queued under D. When the vacuumed_redirects from another
/// replica are imported, the old single-hop chase would resolve D→C and stop,
/// but C is also a redirect. With path compression, D resolves all the way to
/// A, and X gets applied.
#[test]
fn test_merge_multihop_redirect_does_not_strand() {
    // replica_a: build chain root ← A ← B ← C ← D, then delete B, C, D and vacuum.
    let mut replica_a = RGADocument::new("actor_a".to_string());
    replica_a.apply_operation(&make_insert("actor_a", 1, "A", Some(ElementId::root())));
    replica_a.apply_operation(&make_insert("actor_a", 2, "B", Some(eid("actor_a", 1))));
    replica_a.apply_operation(&make_insert("actor_a", 3, "C", Some(eid("actor_a", 2))));
    replica_a.apply_operation(&make_insert("actor_a", 4, "D", Some(eid("actor_a", 3))));
    assert_eq!(replica_a.get_text(), "ABCD");

    // Delete B, C, D — but NOT A
    replica_a.apply_operation(&make_delete("actor_a", 2));
    replica_a.apply_operation(&make_delete("actor_a", 3));
    replica_a.apply_operation(&make_delete("actor_a", 4));
    assert_eq!(replica_a.get_text(), "A");

    replica_a.vacuum_tombstones();
    assert_eq!(replica_a.get_text(), "A");

    // Verify the redirect chain exists
    assert!(replica_a.is_vacuumed("actor_a", 2));
    assert!(replica_a.is_vacuumed("actor_a", 3));
    assert!(replica_a.is_vacuumed("actor_a", 4));

    // replica_b has A alive and X pending under D (D hasn't arrived)
    let mut replica_b = RGADocument::new("actor_b".to_string());
    replica_b.apply_operation(&make_insert("actor_a", 1, "A", Some(ElementId::root())));

    // X arrives anchored after D, which doesn't exist yet → queued as pending
    let x_insert = make_insert("actor_x", 1, "X", Some(eid("actor_a", 4)));
    replica_b.apply_operation(&x_insert);

    let d_key = eid("actor_a", 4).to_string_key();
    assert!(
        replica_b.pending_inserts.contains_key(&d_key),
        "X should be stranded under D"
    );
    assert!(
        !replica_b.elements.contains_key(&eid("actor_x", 1).to_string_key()),
        "X should NOT be in elements yet"
    );

    // Merge: replica_b imports replica_a's vacuum state (multi-hop redirects)
    replica_b.merge(&replica_a);

    // After merge, X must NOT be stranded. The redirect chain D→...→A should
    // have been fully resolved, and since A is in elements, X should be applied.
    assert!(
        !replica_b.pending_inserts.contains_key(&d_key),
        "D's pending queue should be drained"
    );
    assert!(
        replica_b.elements.contains_key(&eid("actor_x", 1).to_string_key()),
        "X must be applied — multi-hop redirect should resolve D all the way to A"
    );

    let x_elem = replica_b
        .elements
        .get(&eid("actor_x", 1).to_string_key())
        .unwrap();
    assert_eq!(
        x_elem.after_id,
        Some(eid("actor_a", 1)),
        "X should be anchored to A (terminal redirect target)"
    );
    assert!(!x_elem.deleted);
}

/// Regression test for Bug 2: merge-time pending drain must not duplicate ops.
///
/// When an INSERT first arrives with a missing parent, apply_operation (with
/// push_to_ops=true) queues it AND appends it to self.operations. Later, when
/// merge drains the pending queue, it must NOT re-append the same op. The fix
/// is to drain with push_to_ops=false.
#[test]
fn test_merge_pending_drain_does_not_duplicate_ops() {
    let mut replica_c = RGADocument::new("actor_c".to_string());

    // X arrives anchored after A2, which doesn't exist → queued as pending.
    // apply_operation (push_to_ops=true) adds it to operations AND to pending_inserts.
    let x_insert = make_insert("actor_x", 1, "X", Some(eid("actor_a", 2)));
    replica_c.apply_operation(&x_insert);

    let a2_key = eid("actor_a", 2).to_string_key();
    assert!(replica_c.pending_inserts.contains_key(&a2_key));

    // Count how many times X appears in operations BEFORE merge
    let x_eid = eid("actor_x", 1);
    let ops_count_before = replica_c
        .operations
        .iter()
        .filter(|op| op.element_id == x_eid)
        .count();
    assert_eq!(
        ops_count_before, 1,
        "X should appear exactly once in ops after initial queue"
    );

    // Build replica_a with vacuum state that redirects A2→A1
    let mut replica_a = RGADocument::new("actor_a".to_string());
    replica_a.apply_operation(&make_insert("actor_a", 1, "A1", Some(ElementId::root())));
    replica_a.apply_operation(&make_insert("actor_a", 2, "A2", Some(eid("actor_a", 1))));
    replica_a.apply_operation(&make_delete("actor_a", 2));
    replica_a.vacuum_tombstones();

    // Merge: replica_c imports vacuum state, drain resolves X
    replica_c.merge(&replica_a);

    // X should now be applied
    assert!(
        replica_c
            .elements
            .contains_key(&eid("actor_x", 1).to_string_key()),
        "X should be applied after merge drain"
    );

    // Count X in operations AFTER merge — must still be exactly 1 (no duplicate)
    let ops_count_after = replica_c
        .operations
        .iter()
        .filter(|op| op.element_id == x_eid)
        .count();
    assert_eq!(
        ops_count_after, 1,
        "X must appear exactly once in operations — merge drain must NOT re-append"
    );
}

// ── Option A: Merge Physical Removal Tests ─────────────────────────────────

/// Verify that merge step 1b physically removes elements covered by imported
/// vacuumed_intervals. After merge, no element should be both in self.elements
/// AND covered by vacuumed_intervals.
#[test]
fn test_merge_imported_vacuum_removes_elements() {
    // replica_a has elements A, B, C
    let mut replica_a = RGADocument::new("actor_a".to_string());
    replica_a.apply_operation(&make_insert("actor_a", 1, "A", Some(ElementId::root())));
    replica_a.apply_operation(&make_insert("actor_a", 2, "B", Some(eid("actor_a", 1))));
    replica_a.apply_operation(&make_insert("actor_a", 3, "C", Some(eid("actor_a", 2))));
    assert_eq!(replica_a.get_text(), "ABC");

    // replica_b has the same elements, deletes B, and vacuums
    let mut replica_b = RGADocument::new("actor_b".to_string());
    replica_b.apply_operation(&make_insert("actor_a", 1, "A", Some(ElementId::root())));
    replica_b.apply_operation(&make_insert("actor_a", 2, "B", Some(eid("actor_a", 1))));
    replica_b.apply_operation(&make_insert("actor_a", 3, "C", Some(eid("actor_a", 2))));
    replica_b.apply_operation(&make_delete("actor_a", 2));
    replica_b.vacuum_tombstones();
    assert_eq!(replica_b.get_text(), "AC");

    // replica_a merges replica_b — should physically remove B, not just mark deleted
    replica_a.merge(&replica_b);

    // Physical exclusivity: B must NOT be in elements
    assert!(
        !replica_a.elements.contains_key(&eid("actor_a", 2).to_string_key()),
        "B must be physically removed from elements after merge (Option A)"
    );

    // But B IS in vacuumed_intervals
    assert!(replica_a.is_vacuumed("actor_a", 2));

    // Text is correct
    assert_eq!(replica_a.get_text(), "AC");

    // No element in elements is covered by vacuumed_intervals (strict exclusivity)
    for key in replica_a.elements.keys() {
        if let Some(colon_pos) = key.find(':') {
            let replica = &key[..colon_pos];
            if let Ok(seq) = key[colon_pos + 1..].parse::<i64>() {
                assert!(
                    !replica_a.is_vacuumed(replica, seq),
                    "Element {} should not be in both elements and vacuumed_intervals",
                    key
                );
            }
        }
    }
}

/// Verify that merge step 1b rewrites parent pointers of surviving elements
/// when their after_id references a vacuumed element.
#[test]
fn test_merge_imported_vacuum_rewrites_parents() {
    // Chain: root <- A <- B <- C. B will be vacuumed remotely.
    let mut replica_a = RGADocument::new("actor_a".to_string());
    replica_a.apply_operation(&make_insert("actor_a", 1, "A", Some(ElementId::root())));
    replica_a.apply_operation(&make_insert("actor_a", 2, "B", Some(eid("actor_a", 1))));
    replica_a.apply_operation(&make_insert("actor_a", 3, "C", Some(eid("actor_a", 2))));
    assert_eq!(replica_a.get_text(), "ABC");

    // C's after_id is B before merge
    let c_before = replica_a.elements.get(&eid("actor_a", 3).to_string_key()).unwrap();
    assert_eq!(c_before.after_id, Some(eid("actor_a", 2)));

    // replica_b vacuums B with redirect B -> A
    let mut replica_b = RGADocument::new("actor_b".to_string());
    replica_b.apply_operation(&make_insert("actor_a", 1, "A", Some(ElementId::root())));
    replica_b.apply_operation(&make_insert("actor_a", 2, "B", Some(eid("actor_a", 1))));
    replica_b.apply_operation(&make_insert("actor_a", 3, "C", Some(eid("actor_a", 2))));
    replica_b.apply_operation(&make_delete("actor_a", 2));
    replica_b.vacuum_tombstones();

    // Merge
    replica_a.merge(&replica_b);

    // C's after_id must now be A (rewritten by step 1b)
    let c_after = replica_a.elements.get(&eid("actor_a", 3).to_string_key()).unwrap();
    assert_eq!(
        c_after.after_id,
        Some(eid("actor_a", 1)),
        "C's parent should be rewritten from B to A during merge step 1b"
    );

    // And B is physically gone
    assert!(!replica_a.elements.contains_key(&eid("actor_a", 2).to_string_key()));
}

/// Verify that step 1b (physical removal) and step 3 (pending drain) don't
/// conflict: a pending insert under a vacuumed parent should still be resolved
/// even after the parent is physically removed.
#[test]
fn test_merge_imported_vacuum_with_pending_drain_interaction() {
    // replica_c has X pending under A2 (A2 hasn't arrived yet)
    let mut replica_c = RGADocument::new("actor_c".to_string());
    // Give C knowledge of A1 so the redirect target exists
    replica_c.apply_operation(&make_insert("actor_a", 1, "A1", Some(ElementId::root())));

    let x_insert = make_insert("actor_x", 1, "X", Some(eid("actor_a", 2)));
    replica_c.apply_operation(&x_insert);
    assert!(replica_c.pending_inserts.contains_key(&eid("actor_a", 2).to_string_key()));

    // replica_a builds A1, A2, deletes A2, vacuums (redirect A2 -> A1)
    let mut replica_a = RGADocument::new("actor_a".to_string());
    replica_a.apply_operation(&make_insert("actor_a", 1, "A1", Some(ElementId::root())));
    replica_a.apply_operation(&make_insert("actor_a", 2, "A2", Some(eid("actor_a", 1))));
    replica_a.apply_operation(&make_delete("actor_a", 2));
    replica_a.vacuum_tombstones();

    // Merge: step 1b removes A2 from elements (if C ever received it via ops replay),
    // step 3 drains pending X with redirect A2 -> A1
    replica_c.merge(&replica_a);

    // X should be applied with after_id = A1
    assert!(
        replica_c.elements.contains_key(&eid("actor_x", 1).to_string_key()),
        "X should be applied after merge"
    );
    let x_elem = replica_c.elements.get(&eid("actor_x", 1).to_string_key()).unwrap();
    assert_eq!(
        x_elem.after_id,
        Some(eid("actor_a", 1)),
        "X should be anchored to A1 via redirect"
    );

    // A2 should not be in elements
    assert!(!replica_c.elements.contains_key(&eid("actor_a", 2).to_string_key()));
    // A2 should be in vacuumed_intervals
    assert!(replica_c.is_vacuumed("actor_a", 2));
}

// ── Decisive Property Test: Random Merge-Order Convergence ─────────────────

/// Generate random interleavings of {insert, delete, vacuum, merge} across
/// 3 replicas. Assert that once all replicas synchronize all data, their
/// physical elements maps are structurally identical.
#[test]
fn test_random_merge_order_convergence() {
    use std::collections::BTreeMap;

    // Use a deterministic seed for reproducibility
    fn simple_rng(seed: u64) -> impl FnMut() -> u64 {
        let mut state = seed;
        move || {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            state
        }
    }

    for seed in 0..50 {
        let mut rng = simple_rng(seed);

        // Create 3 replicas
        let mut replicas = vec![
            RGADocument::new("r0".to_string()),
            RGADocument::new("r1".to_string()),
            RGADocument::new("r2".to_string()),
        ];

        let num_ops = 12 + (rng() % 8) as usize; // 12-19 ops per run

        for _ in 0..num_ops {
            let replica_idx = (rng() % 3) as usize;
            let action = rng() % 100;

            if action < 40 {
                // INSERT: pick a random parent from existing active elements
                let active: Vec<ElementId> = replicas[replica_idx]
                    .order
                    .iter()
                    .filter_map(|eid| {
                        let elem = replicas[replica_idx].elements.get(&eid.to_string_key())?;
                        if !elem.deleted { Some(eid.clone()) } else { None }
                    })
                    .collect();
                let parent = if active.is_empty() {
                    ElementId::root()
                } else {
                    active[(rng() as usize) % active.len()].clone()
                };
                let seq = replicas[replica_idx].sequence + 1;
                let op = make_insert(
                    &format!("r{}", replica_idx),
                    seq,
                    &format!("v{}", seq),
                    Some(parent),
                );
                replicas[replica_idx].apply_operation(&op);
            } else if action < 70 {
                // DELETE: pick a random active element
                let active: Vec<ElementId> = replicas[replica_idx]
                    .order
                    .iter()
                    .filter_map(|eid| {
                        let elem = replicas[replica_idx].elements.get(&eid.to_string_key())?;
                        if !elem.deleted && *eid != ElementId::root() {
                            Some(eid.clone())
                        } else {
                            None
                        }
                    })
                    .collect();
                if let Some(target) = active.get((rng() as usize) % active.len().max(1)) {
                    let op = make_delete(&target.replica_id, target.sequence);
                    replicas[replica_idx].apply_operation(&op);
                }
            } else if action < 85 {
                // VACUUM
                replicas[replica_idx].vacuum_tombstones();
            } else {
                // MERGE: pick two different replicas
                let src = (rng() % 3) as usize;
                if src != replica_idx {
                    let src_clone = replicas[src].clone();
                    replicas[replica_idx].merge(&src_clone);
                }
            }
        }

        // Final synchronization: merge all replicas in every direction
        // Use multiple rounds to ensure full convergence
        for _ in 0..3 {
            let snapshots = replicas.clone();
            for (i, replica) in replicas.iter_mut().enumerate() {
                for (j, snapshot) in snapshots.iter().enumerate() {
                    if i != j {
                        replica.merge(snapshot);
                    }
                }
            }
        }

        // After full sync, vacuum all replicas to the same physical state
        for r in &mut replicas {
            r.vacuum_tombstones();
        }

        // Assert convergence: all texts must be identical
        let text_0 = replicas[0].get_text();
        let text_1 = replicas[1].get_text();
        let text_2 = replicas[2].get_text();
        assert_eq!(text_0, text_1, "seed={}: r0 and r1 text diverged", seed);
        assert_eq!(text_1, text_2, "seed={}: r1 and r2 text diverged", seed);

        // Assert physical exclusivity: no element in elements is covered by vacuumed_intervals
        for (ri, r) in replicas.iter().enumerate() {
            for key in r.elements.keys() {
                if let Some(colon_pos) = key.find(':') {
                    let replica = &key[..colon_pos];
                    if let Ok(seq) = key[colon_pos + 1..].parse::<i64>() {
                        assert!(
                            !r.is_vacuumed(replica, seq),
                            "seed={}: replica r{} has element {} in both elements and vacuumed_intervals",
                            seed, ri, key
                        );
                    }
                }
            }
        }

        // Assert structural identity: elements maps have the same keys
        let keys_0: BTreeMap<String, String> = replicas[0]
            .elements
            .iter()
            .map(|(k, e)| (k.clone(), format!("{}:{}", e.value, e.deleted)))
            .collect();
        let keys_1: BTreeMap<String, String> = replicas[1]
            .elements
            .iter()
            .map(|(k, e)| (k.clone(), format!("{}:{}", e.value, e.deleted)))
            .collect();
        let keys_2: BTreeMap<String, String> = replicas[2]
            .elements
            .iter()
            .map(|(k, e)| (k.clone(), format!("{}:{}", e.value, e.deleted)))
            .collect();
        assert_eq!(keys_0, keys_1, "seed={}: r0 and r1 elements diverged", seed);
        assert_eq!(keys_1, keys_2, "seed={}: r1 and r2 elements diverged", seed);

        // Assert state hash convergence
        let hash_0 = replicas[0].get_state_hash();
        let hash_1 = replicas[1].get_state_hash();
        let hash_2 = replicas[2].get_state_hash();
        assert_eq!(hash_0, hash_1, "seed={}: r0 and r1 hash diverged", seed);
        assert_eq!(hash_1, hash_2, "seed={}: r1 and r2 hash diverged", seed);
    }
}

// =============================================================================
// Bounded vacuum with frontier-derived meet vectors
// =============================================================================

/// Width=1 linear frontier: the meet equals the full causal_context.
/// A delete dot covered by the meet IS vacuumed.
#[test]
fn test_bounded_vacuum_linear_frontier_vacuums() {
    use crdt::causality::{Dot, VersionVector};
    use crdt::head_crdt::{HeadEvent, HeadFrontier};

    // --- Build an RGA document: root <- A <- B <- C, then delete B ---
    let mut doc = RGADocument::new("actor_a".to_string());
    doc.apply_operation(&make_insert("actor_a", 1, "A", Some(ElementId::root())));
    doc.apply_operation(&make_insert("actor_a", 2, "B", Some(eid("actor_a", 1))));
    doc.apply_operation(&make_insert("actor_a", 3, "C", Some(eid("actor_a", 2))));
    assert_eq!(doc.get_text(), "ABC");

    // Stamp the delete with a promotion dot (agent_x, seq 5)
    let delete_dot = Dot::new("agent_x", 5);
    let mut del_op = make_delete("actor_a", 2);
    del_op.deleted_by = Some(delete_dot.clone());
    doc.apply_operation(&del_op);
    // Also stamp the element's deleted_by directly (simulating promotion-path stamping)
    let b_key = eid("actor_a", 2).to_string_key();
    doc.elements.get_mut(&b_key).unwrap().deleted_by = Some(delete_dot.clone());
    assert_eq!(doc.get_text(), "AC");

    // --- Build a width=1 linear frontier that has observed (agent_x, 5) ---
    let mut frontier = HeadFrontier::new();
    let mut ctx = VersionVector::new();
    ctx.versions.insert("agent_x".to_string(), 5); // has seen up to seq 5
    ctx.versions.insert("agent_y".to_string(), 3);  // some other agent
    frontier.append(HeadEvent {
        snapshot_id: "snap_1".to_string(),
        dot: Dot::new("agent_y", 4), // this promotion itself
        context: ctx,
        agent_id: "agent_y".to_string(),
        frozen_trust: 0.9,
    });
    assert_eq!(frontier.events.len(), 1, "frontier width should be 1");

    // meet == causal_context for width=1
    let meet = frontier.meet_frontier();
    assert!(meet.includes(&delete_dot), "width=1 meet must cover (agent_x, 5)");

    // --- Bounded vacuum with this meet: tombstone SHOULD be removed ---
    let tombstones_before: Vec<_> = doc.elements.values().filter(|e| e.deleted).collect();
    assert_eq!(tombstones_before.len(), 1, "one tombstone before vacuum");

    doc.vacuum_tombstones_bounded(Some(&meet));

    let tombstones_after: Vec<_> = doc.elements.values().filter(|e| e.deleted).collect();
    assert_eq!(tombstones_after.len(), 0, "width=1 linear meet covers delete dot → tombstone vacuumed");
    assert_eq!(doc.get_text(), "AC");
}

/// Width>1 forked frontier: two concurrent branches.
/// A delete dot stamped by one branch is NOT covered by the meet
/// (the other branch hasn't observed it). The tombstone is preserved.
#[test]
fn test_bounded_vacuum_forked_frontier_preserves() {
    use crdt::causality::{Dot, VersionVector};
    use crdt::head_crdt::{HeadEvent, HeadFrontier};

    // --- Build an RGA document: root <- A <- B <- C, then delete B ---
    let mut doc = RGADocument::new("actor_a".to_string());
    doc.apply_operation(&make_insert("actor_a", 1, "A", Some(ElementId::root())));
    doc.apply_operation(&make_insert("actor_a", 2, "B", Some(eid("actor_a", 1))));
    doc.apply_operation(&make_insert("actor_a", 3, "C", Some(eid("actor_a", 2))));
    assert_eq!(doc.get_text(), "ABC");

    // Delete B, stamped by agent_x at promotion seq 5
    let delete_dot = Dot::new("agent_x", 5);
    let mut del_op = make_delete("actor_a", 2);
    del_op.deleted_by = Some(delete_dot.clone());
    doc.apply_operation(&del_op);
    doc.elements
        .get_mut(&eid("actor_a", 2).to_string_key())
        .unwrap()
        .deleted_by = Some(delete_dot.clone());
    assert_eq!(doc.get_text(), "AC");

    // --- Build a width=2 forked frontier ---
    let mut frontier = HeadFrontier::new();

    // Branch 1: agent_x promoted at (agent_x, 5), has seen (agent_y, 2)
    let mut ctx1 = VersionVector::new();
    ctx1.versions.insert("agent_x".to_string(), 4); // agent_x's prior history
    ctx1.versions.insert("agent_y".to_string(), 2);
    frontier.append(HeadEvent {
        snapshot_id: "snap_branch1".to_string(),
        dot: Dot::new("agent_x", 5),
        context: ctx1,
        agent_id: "agent_x".to_string(),
        frozen_trust: 0.9,
    });

    // Branch 2: agent_y promoted at (agent_y, 3), has seen (agent_x, 3) but NOT (agent_x, 5)
    let mut ctx2 = VersionVector::new();
    ctx2.versions.insert("agent_x".to_string(), 3); // only up to seq 3, NOT 5
    ctx2.versions.insert("agent_y".to_string(), 2);
    frontier.append(HeadEvent {
        snapshot_id: "snap_branch2".to_string(),
        dot: Dot::new("agent_y", 3),
        context: ctx2,
        agent_id: "agent_y".to_string(),
        frozen_trust: 0.8,
    });

    // Verify the frontier is genuinely forked (width 2)
    assert_eq!(frontier.events.len(), 2, "frontier should be width 2 (forked)");

    // Compute the meet: component-wise minimum across all branches
    let meet = frontier.meet_frontier();

    // The meet for agent_x should be min(5, 3) = 3 — does NOT cover (agent_x, 5)
    assert!(
        !meet.includes(&delete_dot),
        "forked meet must NOT cover (agent_x, 5); agent_y only saw up to (agent_x, 3)"
    );

    // --- Bounded vacuum with this meet: tombstone must be PRESERVED ---
    let tombstones_before: Vec<_> = doc.elements.values().filter(|e| e.deleted).collect();
    assert_eq!(tombstones_before.len(), 1, "one tombstone before vacuum");

    doc.vacuum_tombstones_bounded(Some(&meet));

    let tombstones_after: Vec<_> = doc.elements.values().filter(|e| e.deleted).collect();
    assert_eq!(
        tombstones_after.len(),
        1,
        "forked meet excludes delete dot → tombstone PRESERVED"
    );
    assert_eq!(doc.get_text(), "AC", "text still correct after bounded vacuum");

    // Verify the tombstone's deleted_by is still intact
    let b_elem = doc.elements.get(&eid("actor_a", 2).to_string_key()).unwrap();
    assert!(b_elem.deleted, "B is still tombstoned");
    assert_eq!(
        b_elem.deleted_by.as_ref().unwrap(),
        &delete_dot,
        "deleted_by provenance is preserved"
    );

    // --- Sanity: if we later converge to width=1 covering (agent_x, 5), vacuum succeeds ---
    let mut converged_meet = VersionVector::new();
    converged_meet.versions.insert("agent_x".to_string(), 5);
    converged_meet.versions.insert("agent_y".to_string(), 3);
    assert!(converged_meet.includes(&delete_dot));

    doc.vacuum_tombstones_bounded(Some(&converged_meet));

    let tombstones_final: Vec<_> = doc.elements.values().filter(|e| e.deleted).collect();
    assert_eq!(
        tombstones_final.len(),
        0,
        "once all branches converge, tombstone is vacuumed"
    );
    assert_eq!(doc.get_text(), "AC");
}
