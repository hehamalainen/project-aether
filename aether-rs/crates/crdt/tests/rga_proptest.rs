use crdt::rga::{RGADocument, RGAOperation};
use proptest::prelude::*;
use rand::rngs::StdRng;
use rand::seq::SliceRandom;
use rand::Rng;
use rand::SeedableRng;

proptest! {
    #[test]
    fn test_crdt_adversarial_chaos(
        seed in any::<u64>(),
        num_agents in 2..5usize,
        num_steps in 10..50usize,
    ) {
        let mut rng = StdRng::seed_from_u64(seed);

        let mut agents: Vec<RGADocument> = (0..num_agents)
            .map(|i| RGADocument::new(format!("agent_{}", i)))
            .collect();

        let mut global_ops: Vec<RGAOperation> = Vec::new();
        let chars = ['A', 'B', 'C', 'D', 'E', 'X', 'Y', 'Z'];

        // Generate and execute a random sequence of actions
        for _ in 0..num_steps {
            let action_type = rng.gen_range(0..100);
            let agent_idx = rng.gen_range(0..num_agents);
            let doc = &mut agents[agent_idx];

            if action_type < 40 {
                // Local Insert
                let pos = if doc.get_length() == 0 { 0 } else { rng.gen_range(0..=doc.get_length()) };
                let ch = chars[rng.gen_range(0..chars.len())];
                let op = doc.local_insert(pos, ch.to_string());
                global_ops.push(op);
            } else if action_type < 60 {
                // Local Delete
                if doc.get_length() > 0 {
                    let pos = rng.gen_range(0..doc.get_length());
                    if let Some(op) = doc.local_delete(pos) {
                        global_ops.push(op);
                    }
                }
            } else {
                // Sync a random operation from the global pool (might be out of order, missing parent!)
                if !global_ops.is_empty() {
                    let op_idx = rng.gen_range(0..global_ops.len());
                    let op = &global_ops[op_idx];
                    doc.apply_operation(op);
                }
            }
        }

        // The chaos has finished. Now we ensure convergence by fully syncing all agents.
        // We will take ALL global ops, shuffle them, and apply them to 3 fresh replicas
        // to prove that delivery order doesn't matter, even with missing parents queued.

        let mut shuffle_1 = global_ops.clone();
        shuffle_1.shuffle(&mut rng);

        let mut shuffle_2 = global_ops.clone();
        shuffle_2.shuffle(&mut rng);

        let mut shuffle_3 = global_ops.clone();
        shuffle_3.shuffle(&mut rng);

        let mut final_1 = RGADocument::new("validator_1".to_string());
        let mut final_2 = RGADocument::new("validator_2".to_string());
        let mut final_3 = RGADocument::new("validator_3".to_string());

        final_1.apply_operations(&shuffle_1);
        final_2.apply_operations(&shuffle_2);
        final_3.apply_operations(&shuffle_3);

        let text1 = final_1.get_text();
        let text2 = final_2.get_text();
        let text3 = final_3.get_text();

        prop_assert_eq!(&text1, &text2);
        prop_assert_eq!(&text2, &text3);

        let hash1 = final_1.get_state_hash();
        let hash2 = final_2.get_state_hash();
        let hash3 = final_3.get_state_hash();

        prop_assert_eq!(&hash1, &hash2);
        prop_assert_eq!(&hash2, &hash3);

        // Ensure the original agents converge if we dump the final shuffled ops into them
        for agent in &mut agents {
            agent.apply_operations(&shuffle_1);
            prop_assert_eq!(agent.get_state_hash(), hash1.clone());
        }

        // --- Phase 4 Durability Tests ---
        // 1. Serde Roundtrip: Serialize final_1 to JSON and back, ensure it matches
        let json_str = serde_json::to_string(&final_1).unwrap();
        let final_1_restored: RGADocument = serde_json::from_str(&json_str).unwrap();
        prop_assert_eq!(final_1_restored.get_text(), text1.clone());
        prop_assert_eq!(final_1_restored.get_state_hash(), hash1.clone());

        // 2. Merge Test: If we merge final_2 into an empty replica, it should reach the same state
        let mut merged_replica = RGADocument::new("validator_merge".to_string());
        merged_replica.merge(&final_2);
        prop_assert_eq!(merged_replica.get_text(), text1.clone());
        prop_assert_eq!(merged_replica.get_state_hash(), hash1.clone());

        // 3. Serde + Merge test for out-of-order quarantined deletions
        // Prove that the restored node retained its known_deletions during merge
        let mut merged_restored = RGADocument::new("validator_merge_restored".to_string());
        merged_restored.merge(&final_1_restored);
        prop_assert_eq!(merged_restored.get_text(), text1.clone());
        prop_assert_eq!(merged_restored.get_state_hash(), hash1.clone());

        // 4. Copilot Round 4 Fix: pending_inserts serde roundtrip durability
        // We synthesize an out-of-order insert missing its parent.
        use crdt::rga::{ElementId, OperationType};
        let missing_parent_id = ElementId { replica_id: "fake".to_string(), sequence: 999 };
        let queued_child_id = ElementId { replica_id: "fake".to_string(), sequence: 1000 };

        let queued_op = RGAOperation {
            op_type: OperationType::INSERT,
            element_id: queued_child_id.clone(),
            value: Some("Q".to_string()),
            after_id: Some(missing_parent_id.clone()),
            timestamp: 0.0,
            deleted_by: None,
        };

        let mut doc_with_pending = final_1.clone();
        doc_with_pending.apply_operation(&queued_op);

        // Assert it is queued
        prop_assert!(doc_with_pending.pending_inserts.contains_key(&missing_parent_id.to_string_key()));

        // Serialize and Deserialize!
        let json_pending = serde_json::to_string(&doc_with_pending).unwrap();
        let mut doc_pending_restored: RGADocument = serde_json::from_str(&json_pending).unwrap();

        // Assert it is STILL queued
        prop_assert!(doc_pending_restored.pending_inserts.contains_key(&missing_parent_id.to_string_key()));

        // Now apply the missing parent!
        let parent_op = RGAOperation {
            op_type: OperationType::INSERT,
            element_id: missing_parent_id.clone(),
            value: Some("P".to_string()),
            after_id: Some(ElementId::root()),
            timestamp: 0.0,
            deleted_by: None,
        };

        doc_pending_restored.apply_operation(&parent_op);
        doc_with_pending.apply_operation(&parent_op);

        // Assert the child was automatically drained and applied!
        let restored_text = doc_pending_restored.get_text();
        prop_assert_eq!(restored_text, doc_with_pending.get_text());
    }
}

#[test]
fn test_replace_tokens_alignment() {
    let mut doc = RGADocument::new("actor_1".to_string());
    
    // Initial load
    let tokens_1 = vec!["foo".to_string(), "bar".to_string(), "baz".to_string()];
    let _ops_1 = doc.replace_tokens(&tokens_1);
    
    // Check initial RGA order and text
    assert_eq!(doc.get_text(), "foobarbaz");
    let foo_id = doc.elements.iter().find(|(_, elem)| elem.value == "foo").unwrap().1.id.clone();
    let bar_id = doc.elements.iter().find(|(_, elem)| elem.value == "bar").unwrap().1.id.clone();
    let baz_id = doc.elements.iter().find(|(_, elem)| elem.value == "baz").unwrap().1.id.clone();
    
    // Replace with a minor change: "bar" -> "qux"
    let tokens_2 = vec!["foo".to_string(), "qux".to_string(), "baz".to_string()];
    let ops_2 = doc.replace_tokens(&tokens_2);
    
    // Assert text is correct
    assert_eq!(doc.get_text(), "fooquxbaz");
    
    // Assert foo and baz retained their identical, stable ElementIds!
    let foo_id_new = doc.elements.iter().find(|(_, elem)| elem.value == "foo" && !elem.deleted).unwrap().1.id.clone();
    let baz_id_new = doc.elements.iter().find(|(_, elem)| elem.value == "baz" && !elem.deleted).unwrap().1.id.clone();
    assert_eq!(foo_id, foo_id_new);
    assert_eq!(baz_id, baz_id_new);
    
    // Assert bar is deleted (tombstoned)
    assert!(doc.elements.get(&bar_id.to_string_key()).unwrap().deleted);
    
    // Assert minimal operations were generated (exactly 1 DELETE and 1 INSERT)
    assert_eq!(ops_2.len(), 2);
    assert!(ops_2.iter().any(|op| op.op_type == crdt::rga::OperationType::DELETE && op.element_id == bar_id));
    assert!(ops_2.iter().any(|op| op.op_type == crdt::rga::OperationType::INSERT && op.value == Some("qux".to_string())));
}

#[test]
fn test_inside_token_splitting() {
    let mut doc = RGADocument::new("actor_1".to_string());
    
    // Initial tokens: "process_data"
    let tokens = vec!["process_data".to_string()];
    doc.replace_tokens(&tokens);
    assert_eq!(doc.get_text(), "process_data");
    
    let original_id = doc.elements.iter().find(|(_, elem)| elem.value == "process_data").unwrap().1.id.clone();
    
    // Split: insert "_proposal" at relative character offset 8 ("process_")
    let ops = doc.local_char_insert_split(8, "_proposal".to_string());
    
    // Assert text is correct
    assert_eq!(doc.get_text(), "process__proposaldata");
    
    // Assert original token is deleted (tombstoned)
    assert!(doc.elements.get(&original_id.to_string_key()).unwrap().deleted);
    
    // Assert the 3 sub-tokens exist and are active
    let _left_id = doc.elements.iter().find(|(_, elem)| elem.value == "process_" && !elem.deleted).unwrap().1.id.clone();
    let mid_id = doc.elements.iter().find(|(_, elem)| elem.value == "_proposal" && !elem.deleted).unwrap().1.id.clone();
    let _right_id = doc.elements.iter().find(|(_, elem)| elem.value == "data" && !elem.deleted).unwrap().1.id.clone();
    
    // Assert operations structure
    assert_eq!(ops.len(), 4); // 1 DELETE + 3 INSERTs
    
    // Now let's try local deletion within "process__proposaldata"
    // Delete "_proposal" (length 9) starting at offset 8
    let del_ops = doc.local_char_delete_split(8, 9);
    
    // Assert text reverted back to "process_data"
    assert_eq!(doc.get_text(), "process_data");
    assert!(doc.elements.get(&mid_id.to_string_key()).unwrap().deleted);
    assert_eq!(del_ops.len(), 1); // 1 DELETE (boundary deletion of the middle "_proposal" token)
}

// ── Ticket 1: Bounded Sequence Alignment Tests ──────────────────────────────

#[allow(clippy::needless_range_loop)]
fn replace_tokens_full_reference(doc: &mut RGADocument, tokens: &[String]) -> Vec<RGAOperation> {
    use crdt::rga::{ElementId, OperationType, RGAOperation};

    let active_elements: Vec<(ElementId, String)> = doc.order
        .iter()
        .filter_map(|eid| {
            let elem = doc.elements.get(&eid.to_string_key())?;
            if !elem.deleted {
                Some((eid.clone(), elem.value.clone()))
            } else {
                None
            }
        })
        .collect();

    let n = active_elements.len();
    let m = tokens.len();

    let mut dp = vec![vec![0usize; m + 1]; n + 1];

    for i in 0..=n {
        dp[i][0] = i;
    }
    for j in 0..=m {
        dp[0][j] = j;
    }

    for i in 1..=n {
        for j in 1..=m {
            if active_elements[i - 1].1 == tokens[j - 1] {
                dp[i][j] = dp[i - 1][j - 1];
            } else {
                let cost_del = dp[i - 1][j] + 1;
                let cost_ins = dp[i][j - 1] + 1;
                dp[i][j] = cost_del.min(cost_ins);
            }
        }
    }

    #[derive(Debug, Clone, PartialEq, Eq)]
    enum Step {
        Keep { active_idx: usize },
        Delete { active_idx: usize },
        Insert { token_idx: usize },
    }

    let mut i = n;
    let mut j = m;
    let mut steps = Vec::new();

    while i > 0 || j > 0 {
        if j > 0 && dp[i][j] == dp[i][j - 1] + 1 {
            steps.push(Step::Insert { token_idx: j - 1 });
            j -= 1;
        } else if i > 0 && j > 0 && active_elements[i - 1].1 == tokens[j - 1] && dp[i][j] == dp[i - 1][j - 1] {
            steps.push(Step::Keep { active_idx: i - 1 });
            i -= 1;
            j -= 1;
        } else if i > 0 && dp[i][j] == dp[i - 1][j] + 1 {
            steps.push(Step::Delete { active_idx: i - 1 });
            i -= 1;
        } else {
            if i > 0 && j > 0 {
                steps.push(Step::Delete { active_idx: i - 1 });
                steps.push(Step::Insert { token_idx: j - 1 });
                i -= 1;
                j -= 1;
            } else if i > 0 {
                steps.push(Step::Delete { active_idx: i - 1 });
                i -= 1;
            } else {
                steps.push(Step::Insert { token_idx: j - 1 });
                j -= 1;
            }
        }
    }

    steps.reverse();

    let mut generated_ops = Vec::new();
    let mut prev_id = ElementId::root();

    for step in steps {
        match step {
            Step::Keep { active_idx } => {
                prev_id = active_elements[active_idx].0.clone();
            }
            Step::Delete { active_idx } => {
                let id_to_delete = active_elements[active_idx].0.clone();
                let op = RGAOperation {
                    op_type: OperationType::DELETE,
                    element_id: id_to_delete,
                    value: None,
                    after_id: None,
                    timestamp: 0.0,
                    deleted_by: None,
                };
                doc.apply_operation(&op);
                generated_ops.push(op);
            }
            Step::Insert { token_idx } => {
                doc.sequence += 1;
                let next_id = ElementId {
                    replica_id: doc.replica_id.clone(),
                    sequence: doc.sequence,
                };
                let op = RGAOperation {
                    op_type: OperationType::INSERT,
                    element_id: next_id.clone(),
                    value: Some(tokens[token_idx].clone()),
                    after_id: Some(prev_id.clone()),
                    timestamp: 0.0,
                    deleted_by: None,
                };
                doc.apply_operation(&op);
                generated_ops.push(op);
                prev_id = next_id;
            }
        }
    }

    generated_ops
}

#[test]
#[allow(clippy::useless_vec)]
fn test_bounded_alignment_matches_full() {
    // We test several permutations to compare the output text, order, and state hashes
    // between the optimized bounded alignment (prefix/suffix stripping) and the standard
    // full alignment reference.
    let initial_tokens = vec![
        "alpha".to_string(),
        "beta".to_string(),
        "gamma".to_string(),
        "delta".to_string(),
        "epsilon".to_string(),
    ];

    let test_cases = vec![
        // Case 1: Simple edit in the middle (should strip prefix/suffix, optimization active)
        vec![
            "alpha".to_string(),
            "beta".to_string(),
            "gamma_mutated".to_string(),
            "delta".to_string(),
            "epsilon".to_string(),
        ],
        // Case 2: Multi-edit in the middle
        vec![
            "alpha".to_string(),
            "beta".to_string(),
            "gamma_mutated".to_string(),
            "delta_mutated".to_string(),
            "epsilon".to_string(),
        ],
        // Case 3: Edits at boundary (should trigger fallback/prevent optimization boundary errors)
        vec![
            "alpha_mutated".to_string(),
            "beta".to_string(),
            "gamma".to_string(),
            "delta".to_string(),
            "epsilon_mutated".to_string(),
        ],
        // Case 4: Complete change (optimization prefix/suffix len is 0)
        vec![
            "one".to_string(),
            "two".to_string(),
            "three".to_string(),
        ],
    ];

    for (case_idx, mutated) in test_cases.iter().enumerate() {
        let mut doc_bounded = RGADocument::new("actor_1".to_string());
        let mut doc_full = RGADocument::new("actor_1".to_string()); // use same replica_id

        doc_bounded.replace_tokens(&initial_tokens);
        doc_full.replace_tokens(&initial_tokens);

        doc_bounded.replace_tokens(mutated);
        replace_tokens_full_reference(&mut doc_full, mutated);

        // Verify the resulting text is identical
        assert_eq!(
            doc_bounded.get_text(),
            doc_full.get_text(),
            "Case {}: Text mismatch between bounded and full alignment",
            case_idx
        );

        // Verify state hashes match perfectly
        assert_eq!(
            doc_bounded.get_state_hash(),
            doc_full.get_state_hash(),
            "Case {}: Hash mismatch between bounded and full alignment",
            case_idx
        );
    }
}

#[test]
fn test_bounded_alignment_overlap_margin() {
    let mut doc = RGADocument::new("actor_1".to_string());
    let initial = vec!["A".to_string(), "B".to_string(), "C".to_string(), "D".to_string(), "E".to_string()];
    doc.replace_tokens(&initial);

    // Modify C -> X. This has a prefix "A", "B" (len 2) and suffix "D", "E" (len 2).
    // The overlap margin of 1 token means the window will start at index 1 ("B") and end at active index 4 ("D").
    // This checks that active window bounds clamp correctly and include the overlap tokens.
    let mutated = vec!["A".to_string(), "B".to_string(), "X".to_string(), "D".to_string(), "E".to_string()];
    let ops = doc.replace_tokens(&mutated);

    assert_eq!(doc.get_text(), "ABXDE");
    assert_eq!(ops.len(), 2); // 1 delete of C, 1 insert of X
}

#[test]
fn test_bounded_alignment_fallback() {
    let mut doc = RGADocument::new("actor_1".to_string());
    let initial = vec!["A".to_string(), "B".to_string(), "C".to_string()];
    doc.replace_tokens(&initial);

    // If we replace with tokens that trigger fallback (or have no prefix/suffix),
    // it falls back to full alignment. Let's make sure it converges correctly.
    let mutated = vec!["X".to_string(), "Y".to_string(), "Z".to_string()];
    let ops = doc.replace_tokens(&mutated);

    assert_eq!(doc.get_text(), "XYZ");
    // Since everything changed, it should delete all 3 and insert all 3
    assert_eq!(ops.len(), 6);
}


