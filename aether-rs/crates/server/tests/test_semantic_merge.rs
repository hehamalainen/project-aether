#[cfg(feature = "db-postgres")]
static NEXT_PORT: std::sync::atomic::AtomicU16 = std::sync::atomic::AtomicU16::new(8400);

#[cfg(feature = "db-postgres")]
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn test_serialized_semantic_merge_lane() {
    use reqwest::Client;
    use sqlx::postgres::PgPoolOptions;
    use std::time::Duration;
    use tokio::process::Command;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;

    let db_url = std::env::var("DATABASE_URL").expect("DATABASE_URL must be set to run pg tests");

    let pool = PgPoolOptions::new()
        .max_connections(50)
        .connect(&db_url)
        .await
        .expect("Failed to connect to PG DB. Check your DATABASE_URL.");

    let port = NEXT_PORT.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
    
    let current_dir = std::env::current_dir().unwrap();
    let mut binary_path = current_dir.join("target/debug/server");
    if !binary_path.exists() {
        binary_path = current_dir.join("../../target/debug/server");
    }
    let mut server_process = Command::new(binary_path)
        .env("DATABASE_URL", &db_url)
        .env("PORT", port.to_string())
        .env("AETHER_CRDT_MODE", "frontier")
        .spawn()
        .expect("Failed to spawn server process");

    let client = Client::new();
    let mut ready = false;
    for _ in 0..30 {
        tokio::time::sleep(Duration::from_millis(500)).await;
        if let Ok(res) = client.get(format!("http://localhost:{}/v1/health", port)).send().await {
            if res.status().is_success() {
                ready = true;
                break;
            }
        }
    }

    if !ready {
        let _ = server_process.kill().await;
        panic!("Server did not start in time on port {}", port);
    }

    // 1. Create a Repository and Enable CRDT mode
    let repo_res = client
        .post(format!("http://localhost:{}/v1/repos", port))
        .json(&serde_json::json!({
            "name": format!("semantic_merge_test_repo_{}", uuid::Uuid::new_v4().simple()),
            "description": "Repo for semantic merge CRDT test"
        }))
        .send()
        .await
        .expect("Failed to create repo");
        
    assert!(repo_res.status().is_success());
    let repo_json: serde_json::Value = repo_res.json().await.unwrap();
    let repo_id = repo_json["id"].as_str().unwrap().to_string();

    // Enable CRDT
    sqlx::query("UPDATE aether_repositories SET crdt_enabled = true WHERE id = $1")
        .bind(&repo_id)
        .execute(&pool)
        .await
        .unwrap();

    let initial_head_seq = repo_json["head_sequence"].as_i64().unwrap();

    // 2. Submit concurrent proposals
    let num_proposals = 20;
    let mut proposal_ids = Vec::new();
    let agent_id = "test_agent_crdt".to_string();

    for i in 0..num_proposals {
        let prop_res = client
            .post(format!("http://localhost:{}/v1/repos/{}/proposals", port, repo_id))
            .json(&serde_json::json!({
                "agent_id": format!("{}_{}", agent_id, i),
                "task_id": format!("task_{}", i),
                "proposal_title": "Semantic Merge Sub",
                "proposal_description": "Testing",
                "confidence_score": 0.95,
                "affected_paths": ["src/main.py"],
                "dependencies": [],
                "change_type": "full_file",
                "change_content": "print('Hello CRDT')"
            }))
            .send()
            .await
            .expect("Failed to submit");
        if !prop_res.status().is_success() {
            println!("Failed to submit proposal: {:?}", prop_res.text().await.unwrap());
            panic!("Proposal submission failed");
        }
        let p_json: serde_json::Value = prop_res.json().await.unwrap();
        proposal_ids.push(p_json["proposal_id"].as_str().unwrap().to_string());
    }

    // 3. Concurrently request promotion for all proposals independently (simulating 20 agents pushing at once)
    // Under OCC model, concurrent requests may fail with 409 Conflict and need client-side retry.
    let mut promote_handles = vec![];
    let success_count = Arc::new(AtomicUsize::new(0));
    let conflict_409_count = Arc::new(AtomicUsize::new(0));
    let reject_count = Arc::new(AtomicUsize::new(0));
    let internal_err_count = Arc::new(AtomicUsize::new(0));

    for prop_id in proposal_ids.clone() {
        let client_clone = client.clone();
        let r_id_clone = repo_id.clone();
        let p_id_clone = prop_id.clone();
        let succ_clone = success_count.clone();
        let conf_clone = conflict_409_count.clone();
        let rej_clone = reject_count.clone();
        let err_clone = internal_err_count.clone();
        let server_port = port;

        promote_handles.push(tokio::spawn(async move {
            let mut attempts = 0;
            loop {
                let res = client_clone
                    .post(format!("http://localhost:{}/v1/repos/{}/promotions", server_port, r_id_clone))
                    .json(&serde_json::json!({
                        "proposal_ids": [p_id_clone]
                    }))
                    .send()
                    .await;
                    
                match res {
                    Ok(response) => {
                        let status = response.status();
                        if status == 409 {
                            conf_clone.fetch_add(1, Ordering::SeqCst);
                            attempts += 1;
                            if attempts > 50 {
                                println!("Proposal {} failed after max retries", p_id_clone);
                                break;
                            }
                            let sleep_ms = 40 + (attempts * 10) + (u64::from(p_id_clone.as_bytes()[p_id_clone.len()-1]) % 30);
                            tokio::time::sleep(Duration::from_millis(sleep_ms)).await;
                        } else if status.is_success() {
                            let json: serde_json::Value = response.json().await.unwrap_or(serde_json::json!({}));
                            if json["success"].as_bool().unwrap_or(false) {
                                succ_clone.fetch_add(1, Ordering::SeqCst);
                                break;
                            } else {
                                rej_clone.fetch_add(1, Ordering::SeqCst);
                                break;
                            }
                        } else {
                            rej_clone.fetch_add(1, Ordering::SeqCst);
                            break;
                        }
                    },
                    Err(_) => {
                        err_clone.fetch_add(1, Ordering::SeqCst);
                        break;
                    }
                }
            }
        }));
    }

    for handle in promote_handles {
        let _ = handle.await;
    }

    let _ = server_process.kill().await;

    let successes = success_count.load(Ordering::SeqCst);
    let conflicts = conflict_409_count.load(Ordering::SeqCst);
    let rejects = reject_count.load(Ordering::SeqCst);
    let errors = internal_err_count.load(Ordering::SeqCst);

    println!("Total Requests: {}", num_proposals);
    println!("Successes: {}", successes);
    println!("Conflicts (409): {}", conflicts);
    println!("Rejects (e.g. gates): {}", rejects);
    println!("Errors: {}", errors);

    // Contract Validation
    
    // 1. Exact Request Accounting
    assert_eq!(successes, num_proposals, "All proposals should eventually succeed after retries");
    assert_eq!(rejects, 0, "No proposals should be rejected");
    assert_eq!(errors, 0, "No internal errors");

    // 3. Head Sequence delta matching successful promotions (No Lost Updates)
    let final_repo: (i64,) = sqlx::query_as("SELECT head_sequence FROM aether_repositories WHERE id = $1")
        .bind(&repo_id)
        .fetch_one(&pool)
        .await
        .unwrap();

    let seq_delta = final_repo.0 - initial_head_seq;
    assert_eq!(seq_delta as usize, successes, "Head sequence delta does not match successful promotions (Lost Updates!)");

    // 4. No stranded states
    let final_proposals: Vec<(String, String)> = sqlx::query_as("SELECT id, status FROM aether_proposals WHERE repo_id = $1")
        .bind(&repo_id)
        .fetch_all(&pool)
        .await
        .unwrap();

    for p in final_proposals {
        assert_eq!(p.1, "PROMOTED", "Proposal {} was left in stranded state {}", p.0, p.1);
    }
}

#[cfg(feature = "db-postgres")]
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn test_crdt_mixed_outcomes() {
    use reqwest::Client;
    use sqlx::postgres::PgPoolOptions;
    use std::time::Duration;
    use tokio::process::Command;

    let db_url = std::env::var("DATABASE_URL").expect("DATABASE_URL must be set to run pg tests");

    let pool = PgPoolOptions::new()
        .max_connections(50)
        .connect(&db_url)
        .await
        .expect("Failed to connect to PG DB. Check your DATABASE_URL.");

    let port = NEXT_PORT.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
    
    let current_dir = std::env::current_dir().unwrap();
    let mut binary_path = current_dir.join("target/debug/server");
    if !binary_path.exists() {
        binary_path = current_dir.join("../../target/debug/server");
    }
    let mut server_process = Command::new(binary_path)
        .env("DATABASE_URL", &db_url)
        .env("PORT", port.to_string())
        .env("AETHER_CRDT_MODE", "frontier")
        .spawn()
        .expect("Failed to spawn server process");

    let client = Client::new();
    let mut ready = false;
    for _ in 0..30 {
        tokio::time::sleep(Duration::from_millis(500)).await;
        if let Ok(res) = client.get(format!("http://localhost:{}/v1/health", port)).send().await {
            if res.status().is_success() {
                ready = true;
                break;
            }
        }
    }

    if !ready {
        let _ = server_process.kill().await;
        panic!("Server did not start in time on port {}", port);
    }

    let repo_res = client
        .post(format!("http://localhost:{}/v1/repos", port))
        .json(&serde_json::json!({
            "name": format!("semantic_merge_mixed_{}", uuid::Uuid::new_v4().simple()),
            "description": "Repo for semantic merge mixed test",
            "gate_config": "[{\"name\": \"conditional_fail_gate\", \"command\": \"bash -c \\\"if grep -q Mix_fail src/mixed.py; then exit 1; else exit 0; fi\\\"\", \"required\": true}]"
        }))
        .send()
        .await
        .expect("Failed to create repo");
        
    assert!(repo_res.status().is_success());
    let repo_json: serde_json::Value = repo_res.json().await.unwrap();
    let repo_id = repo_json["id"].as_str().unwrap().to_string();

    sqlx::query("UPDATE aether_repositories SET crdt_enabled = true, gate_config = $1 WHERE id = $2")
        .bind("[{\"name\": \"conditional_fail_gate\", \"command\": \"bash -c \\\"if grep -q Mix_fail src/mixed.py; then exit 1; else exit 0; fi\\\"\", \"required\": true}]")
        .bind(&repo_id)
        .execute(&pool)
        .await
        .unwrap();

    let initial_head_seq = repo_json["head_sequence"].as_i64().unwrap();

    let mut proposal_ids = Vec::new();
    let agent_id = "test_agent_mixed".to_string();

    for i in 0..10 {
        let prop_res = client
            .post(format!("http://localhost:{}/v1/repos/{}/proposals", port, repo_id))
            .json(&serde_json::json!({
                "agent_id": format!("{}_{}", agent_id, i),
                "task_id": format!("task_m_{}", i),
                "proposal_title": "Mixed Sub",
                "proposal_description": "Testing",
                "confidence_score": 0.95,
                "affected_paths": ["src/mixed.py"],
                "dependencies": [],
                "change_type": "full_file",
                "change_content": format!("print('Mix {}')", i)
            }))
            .send()
            .await
            .expect("Failed to submit");
        assert!(prop_res.status().is_success());
        let p_json: serde_json::Value = prop_res.json().await.unwrap();
        proposal_ids.push(p_json["proposal_id"].as_str().unwrap().to_string());
    }

    for i in 10..15 {
        let prop_res = client
            .post(format!("http://localhost:{}/v1/repos/{}/proposals", port, repo_id))
            .json(&serde_json::json!({
                "agent_id": format!("{}_{}", agent_id, i),
                "task_id": format!("task_m_fail_{}", i),
                "proposal_title": "Mixed Sub Fail",
                "proposal_description": "Testing",
                "confidence_score": 0.95,
                "affected_paths": ["src/mixed.py"],
                "dependencies": [],
                "change_type": "full_file",
                "change_content": format!("print('Mix_fail {}')", i)
            }))
            .send()
            .await
            .expect("Failed to submit");
        assert!(prop_res.status().is_success());
        let p_json: serde_json::Value = prop_res.json().await.unwrap();
        proposal_ids.push(p_json["proposal_id"].as_str().unwrap().to_string());
    }

    let mut requests_to_send = Vec::new();
    requests_to_send.extend(proposal_ids[0..5].iter().cloned());
    requests_to_send.extend(proposal_ids[0..5].iter().cloned());
    requests_to_send.extend(proposal_ids[5..10].iter().cloned());
    requests_to_send.extend(proposal_ids[10..15].iter().cloned());

    let mut promote_handles = vec![];
    let mut successes = 0;
    let mut conflicts = 0;
    let mut gate_rejects = 0;
    let mut invalid_state_rejects = 0;
    let mut errors = 0;

    for prop_id in requests_to_send {
        let client_clone = client.clone();
        let r_id_clone = repo_id.clone();
        let p_id_clone = prop_id.clone();
        let server_port = port;

        promote_handles.push(tokio::spawn(async move {
            let mut attempts = 0;
            loop {
                let res = client_clone
                    .post(format!("http://localhost:{}/v1/repos/{}/promotions", server_port, r_id_clone))
                    .json(&serde_json::json!({
                        "proposal_ids": [p_id_clone]
                    }))
                    .send()
                    .await;
                    
                match res {
                    Ok(response) => {
                        let status = response.status();
                        if status == 409 {
                            attempts += 1;
                            if attempts > 50 {
                                return "conflict".to_string();
                            }
                            let sleep_ms = 40 + (attempts * 10) + (u64::from(p_id_clone.as_bytes()[p_id_clone.len()-1]) % 30);
                            tokio::time::sleep(Duration::from_millis(sleep_ms)).await;
                        } else if status.is_success() {
                            let json: serde_json::Value = response.json().await.unwrap_or(serde_json::json!({}));
                            if json["success"].as_bool().unwrap_or(false) {
                                return "success".to_string();
                            } else {
                                let msg = json["message"].as_str().unwrap_or("");
                                if msg.contains("required gates failed") || msg.contains("gates failed") {
                                    return "gate_reject".to_string();
                                } else {
                                    return "invalid_state".to_string();
                                }
                            }
                        } else {
                            return "error".to_string();
                        }
                    },
                    Err(_) => {
                        return "error".to_string();
                    }
                }
            }
        }));
    }

    for handle in promote_handles {
        match handle.await.unwrap().as_str() {
            "success" => successes += 1,
            "conflict" => conflicts += 1,
            "gate_reject" => gate_rejects += 1,
            "invalid_state" => invalid_state_rejects += 1,
            _ => errors += 1,
        }
    }

    let _ = server_process.kill().await;

    println!("Successes: {}", successes);
    println!("Conflicts (409): {}", conflicts);
    println!("Invalid State Rejects: {}", invalid_state_rejects);
    println!("Gate Rejects: {}", gate_rejects);
    println!("Errors: {}", errors);

    let total_invalid = conflicts + invalid_state_rejects;

    assert_eq!(successes + conflicts + invalid_state_rejects + gate_rejects + errors, 20, "Exact request accounting failed");
    assert_eq!(successes, 10, "Expected exactly 10 successes");
    assert_eq!(total_invalid, 5, "Expected exactly 5 invalid-state rejections (409s or 200-rejections)");
    assert_eq!(gate_rejects, 5, "Expected exactly 5 gate rejects");
    assert_eq!(errors, 0, "Expected exactly 0 errors");

    let final_repo: (i64,) = sqlx::query_as("SELECT head_sequence FROM aether_repositories WHERE id = $1")
        .bind(&repo_id)
        .fetch_one(&pool)
        .await
        .unwrap();

    let seq_delta = final_repo.0 - initial_head_seq;
    assert_eq!(seq_delta as usize, successes, "Head sequence delta does not match successful promotions");

    let final_proposals: Vec<(String, String)> = sqlx::query_as("SELECT id, status FROM aether_proposals WHERE repo_id = $1")
        .bind(&repo_id)
        .fetch_all(&pool)
        .await
        .unwrap();

    let mut status_map = std::collections::HashMap::new();
    for p in final_proposals {
        status_map.insert(p.0, p.1);
    }

    // Exact per-proposal mapping assertions
    for (i, pid) in proposal_ids.iter().enumerate().take(10) {
        assert_eq!(
            status_map.get(pid).map(|s| s.as_str()), 
            Some("PROMOTED"), 
            "Proposal {} (index {}) should be PROMOTED", pid, i
        );
    }

    for (i, pid) in proposal_ids.iter().enumerate().take(15).skip(10) {
        assert_eq!(
            status_map.get(pid).map(|s| s.as_str()), 
            Some("REJECTED"), 
            "Proposal {} (index {}) should be REJECTED due to gate failure", pid, i
        );
    }
}

#[cfg(feature = "db-postgres")]
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn test_crdt_lock_hold_telemetry() {
    use reqwest::Client;
    use sqlx::postgres::PgPoolOptions;
    use std::time::Duration;
    use tokio::process::Command;
    use std::sync::Arc;

    let db_url = std::env::var("DATABASE_URL").expect("DATABASE_URL must be set to run pg tests");

    let pool = PgPoolOptions::new()
        .max_connections(50)
        .connect(&db_url)
        .await
        .expect("Failed to connect to PG DB. Check your DATABASE_URL.");

    let port = NEXT_PORT.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
    
    let current_dir = std::env::current_dir().unwrap();
    let mut binary_path = current_dir.join("target/debug/server");
    if !binary_path.exists() {
        binary_path = current_dir.join("../../target/debug/server");
    }
    let mut server_process = Command::new(binary_path)
        .env("DATABASE_URL", &db_url)
        .env("PORT", port.to_string())
        .env("AETHER_CRDT_MODE", "frontier")
        .spawn()
        .expect("Failed to spawn server process");

    let client = Client::new();
    let mut ready = false;
    for _ in 0..30 {
        tokio::time::sleep(Duration::from_millis(500)).await;
        if let Ok(res) = client.get(format!("http://localhost:{}/v1/health", port)).send().await {
            if res.status().is_success() {
                ready = true;
                break;
            }
        }
    }

    if !ready {
        let _ = server_process.kill().await;
        panic!("Server did not start in time on port {}", port);
    }

    let repo_res = client
        .post(format!("http://localhost:{}/v1/repos", port))
        .json(&serde_json::json!({
            "name": format!("semantic_merge_telemetry_{}", uuid::Uuid::new_v4().simple()),
            "description": "Repo for semantic merge telemetry test",
            "gate_config": "[{\"name\": \"sleep_gate\", \"command\": \"sleep 0.5\", \"required\": true}]"
        }))
        .send()
        .await
        .expect("Failed to create repo");
        
    assert!(repo_res.status().is_success());
    let repo_json: serde_json::Value = repo_res.json().await.unwrap();
    let repo_id = repo_json["id"].as_str().unwrap().to_string();

    sqlx::query("UPDATE aether_repositories SET crdt_enabled = true, gate_config = $1 WHERE id = $2")
        .bind("[{\"name\": \"sleep_gate\", \"command\": \"sleep 0.5\", \"required\": true}]")
        .bind(&repo_id)
        .execute(&pool)
        .await
        .unwrap();

    let num_proposals = 4;
    let mut proposal_ids = Vec::new();
    let agent_id = "test_agent_telemetry".to_string();

    for i in 0..num_proposals {
        let prop_res = client
            .post(format!("http://localhost:{}/v1/repos/{}/proposals", port, repo_id))
            .json(&serde_json::json!({
                "agent_id": agent_id,
                "task_id": format!("task_t_{}", i),
                "proposal_title": "Telemetry Sub",
                "proposal_description": "Testing",
                "confidence_score": 0.95,
                "affected_paths": ["src/telemetry.py"],
                "dependencies": [],
                "change_type": "full_file",
                "change_content": format!("print('Telemetry {}')", i)
            }))
            .send()
            .await
            .expect("Failed to submit");
        assert!(prop_res.status().is_success());
        let p_json: serde_json::Value = prop_res.json().await.unwrap();
        proposal_ids.push(p_json["proposal_id"].as_str().unwrap().to_string());
    }

    let mut promote_handles = vec![];
    let timings_collected = Arc::new(tokio::sync::Mutex::new(Vec::new()));


    let pool_clone = pool.clone();
    
    let stop_polling = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let stop_polling_clone = stop_polling.clone();

    use sha2::Digest;
    let mut hasher = sha2::Sha256::new();
    hasher.update(repo_id.as_bytes());
    let result = hasher.finalize();
    let mut key_bytes = [0u8; 8];
    key_bytes.copy_from_slice(&result[0..8]);
    let lock_key = i64::from_be_bytes(key_bytes);
    let lock_key_u64 = lock_key as u64;
    let classid = (lock_key_u64 >> 32) as u32;
    let objid = (lock_key_u64 & 0xFFFFFFFF) as u32;

    let saw_perfect_queue = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let saw_perfect_queue_clone = saw_perfect_queue.clone();

    let poller_handle = tokio::spawn(async move {
        let query_true = format!("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND classid = {} AND objid = {} AND granted = true", classid, objid);
        let query_false = format!("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND classid = {} AND objid = {} AND granted = false", classid, objid);
        
        while !stop_polling_clone.load(std::sync::atomic::Ordering::Relaxed) {
            tokio::time::sleep(Duration::from_millis(50)).await;
            if let (Ok(count_true), Ok(count_false)) = (
                sqlx::query_scalar::<_, i64>(&query_true).fetch_one(&pool_clone).await,
                sqlx::query_scalar::<_, i64>(&query_false).fetch_one(&pool_clone).await
            ) {
                if count_true == 1 && count_false == 3 {
                    saw_perfect_queue_clone.store(true, std::sync::atomic::Ordering::Relaxed);
                }
            }
        }
    });

    let barrier = Arc::new(tokio::sync::Barrier::new(4));

    for prop_id in proposal_ids.clone() {
        let client_clone = client.clone();
        let r_id_clone = repo_id.clone();
        let p_id_clone = prop_id.clone();
        let server_port = port;
        let timings_clone = timings_collected.clone();

        let barrier_clone = barrier.clone();
        
        promote_handles.push(tokio::spawn(async move {
            barrier_clone.wait().await;
            let res = client_clone
                .post(format!("http://localhost:{}/v1/repos/{}/promotions", server_port, r_id_clone))
                .json(&serde_json::json!({
                    "proposal_ids": [p_id_clone]
                }))
                .send()
                .await;
                
            if let Ok(response) = res {
                let status = response.status();
                if status.is_success() || status == 409 {
                    let json: serde_json::Value = response.json().await.unwrap_or(serde_json::json!({}));
                    if let Some(evidence_str) = json["evidence"]["_timings_ms"].as_str() {
                        let parsed: std::collections::HashMap<String, f64> = serde_json::from_str(evidence_str).unwrap();
                        let mut t = timings_clone.lock().await;
                        t.push(parsed);
                    }
                }
            }
        }));
    }

    for handle in promote_handles {
        let _ = handle.await;
    }

    stop_polling.store(true, std::sync::atomic::Ordering::Relaxed);
    let _ = poller_handle.await;

    let _ = server_process.kill().await;

    let timings = timings_collected.lock().await;
    assert_eq!(timings.len(), num_proposals, "Not all requests returned timings evidence");

    for (i, timing) in timings.iter().enumerate() {
        if let (Some(t_hold), Some(t_wait)) = (timing.get("t_lock_hold"), timing.get("t_lock_wait")) {
            println!("Request {}: t_lock_hold = {} ms, t_lock_wait = {} ms", i, t_hold, t_wait);
            assert!(*t_hold < 350.0, "t_lock_hold should be < 350ms under OCC because gates run outside the lock. Actual: {}", t_hold);
        } else {
            println!("Request {}: no lock hold/wait timings available (possibly returned early)", i);
        }
    }
}
