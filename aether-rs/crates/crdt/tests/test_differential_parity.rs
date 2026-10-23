use crdt::document_store::DocumentStore;
use crdt::symbol_adapters::{compute_affected_symbols, check_overlap};
use models::entities::Proposal;

// Helper to create a unified diff string
fn make_diff(filename: &str, old_start: usize, old_count: usize, new_start: usize, new_count: usize, lines: &[&str]) -> String {
    let mut diff = format!("diff --git a/{filename} b/{filename}\n");
    diff += &format!("--- a/{filename}\n");
    diff += &format!("+++ b/{filename}\n");
    diff += &format!("@@ -{old_start},{old_count} +{new_start},{new_count} @@\n");
    for line in lines {
        diff += &format!("{line}\n");
    }
    diff
}

// Helper to create a minimal Proposal entity
fn make_proposal(id: &str, change_type: &str, content: &str, paths: &[&str], confidence: f64) -> Proposal {
    let paths_json = serde_json::to_string(&paths).unwrap();
    
    // We compute symbols using compute_affected_symbols on a dummy doc store
    let mut store = DocumentStore::new("test-repo".to_string());
    if change_type == "full_file" {
        for path in paths {
            store.apply_full_file_bounded(path, "", "init", None);
        }
    }
    
    let path_strings: Vec<String> = paths.iter().map(|s| s.to_string()).collect();
    let symbols = compute_affected_symbols(change_type, content, &path_strings, &store);
    let symbols_json = serde_json::to_string(&symbols).ok();

    Proposal {
        id: id.to_string(),
        repo_id: "test-repo".to_string(),
        agent_id: "test-agent".to_string(),
        task_id: Some("test-task".to_string()),
        source: "agent".to_string(),
        source_run_id: None,
        source_task_id: None,
        source_agent_id: None,
        source_attempt_id: None,
        intent_type: None,
        base_head_id: None,
        base_head_sequence: 0,
        status: models::entities::ProposalStatus::Pending,
        confidence_score: confidence,
        change_type: change_type.to_string(),
        change_content: content.to_string(),
        affected_paths: paths_json,
        affected_symbols: symbols_json,
        risk_tier: 1,
        model_id: None,
        calibration_profile: None,
        rationale: None,
        dependencies: "[]".to_string(),
        requires_tests: false,
        requires_security_scan: false,
        idempotency_key: None,
        submitted_at: chrono::Utc::now(),
        promoted_at: None,
        conflict_set_id: None,
        evaluated_at: None,
    }
}

#[test]
fn test_boundary_hunks_splicing() {
    let mut store = DocumentStore::new("test-repo".to_string());
    store.apply_full_file_bounded("main.py", "line1\nline2\nline3\n", "init", None);

    // Diff 1 at the beginning
    let diff1 = make_diff("main.py", 1, 1, 1, 2, &["-line1", "+new_line1", "+extra_line"]);
    // Diff 2 at the end
    let diff2 = make_diff("main.py", 3, 1, 4, 2, &["-line3", "+new_line3", "+final_line"]);

    let affected = store.apply_diff_batch(&[(&diff1, "prop1"), (&diff2, "prop2")]).unwrap();
    assert!(affected.contains(&"main.py".to_string()));

    let doc = store.documents.get("main.py").unwrap();
    let content = doc.document.get_text();
    let expected = "new_line1\nextra_line\nline2\nnew_line3\nfinal_line\n";
    assert_eq!(content, expected);
}

#[test]
fn test_multi_node_deletions_fallback() {
    let mut store = DocumentStore::new("test-repo".to_string());
    let source = "def foo():\n    pass\n\ndef bar():\n    pass\n";
    store.apply_full_file_bounded("main.py", source, "init", None);

    // Diff that deletes the end of foo and start of bar
    let diff = make_diff("main.py", 2, 3, 2, 1, &["-    pass", "-", "-def bar():", "+# merged function"]);
    
    let path_strings = vec!["main.py".to_string()];
    let symbols = compute_affected_symbols("unified_diff", &diff, &path_strings, &store);
    
    assert!(symbols.contains(&"main.py::__raw__".to_string()));
}

#[test]
fn test_full_file_replacement_wildcards() {
    let mut store = DocumentStore::new("test-repo".to_string());
    store.apply_full_file_bounded("main.py", "def foo():\n    pass\n", "init", None);

    let path_strings = vec!["main.py".to_string()];
    let symbols = compute_affected_symbols("full_file", "new content", &path_strings, &store);

    assert!(symbols.contains(&"main.py::__raw__".to_string()) && symbols.contains(&"main.py::__file__".to_string()));
}

#[test]
fn test_raw_wildcard_conflict() {
    let p1 = make_proposal("prop1", "full_file", "print('hello')", &["main.py"], 0.9);
    let p2 = make_proposal("prop2", "unified_diff", "print('bye')", &["main.py"], 0.8);
    
    assert!(check_overlap(&p1, &p2));
}

#[test]
fn test_same_region_exclusion() {
    let mut store = DocumentStore::new("test-repo".to_string());
    store.apply_full_file_bounded("main.py", "x = 1\n", "init", None);

    let diff1 = make_diff("main.py", 1, 1, 1, 1, &["-x = 1", "+x = 10"]);
    let diff2 = make_diff("main.py", 1, 1, 1, 1, &["-x = 1", "+x = 20"]);

    // If applied together in apply_diff_batch, it must fail with Err (spatial overlap)
    let res = store.apply_diff_batch(&[(&diff1, "prop1"), (&diff2, "prop2")]);
    assert!(res.is_err());
}
