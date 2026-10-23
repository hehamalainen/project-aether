#[cfg(feature = "db-postgres")]
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn test_pg_concurrency_conservation_route() {
    use reqwest::Client;
    use sqlx::postgres::PgPoolOptions;
    use sqlx::Row;
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

    // Pick a guaranteed free port using TcpListener binding
    let port = {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("Failed to bind TcpListener to find free port");
        listener.local_addr().expect("Failed to get local address").port()
    };
    
    let current_dir = std::env::current_dir().unwrap();
    let mut binary_path = current_dir.join("target/debug/server");
    if !binary_path.exists() {
        binary_path = current_dir.join("../../target/debug/server");
    }
    let mut server_process = Command::new(binary_path)
        .env("DATABASE_URL", &db_url)
        .env("PORT", port.to_string())
        .spawn()
        .expect("Failed to spawn server process");

    // Wait for the server to be ready
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

    // 1. Create 20 Repositories
    let num_repos = 20;
    let mut repo_ids = Vec::new();
    for _ in 0..num_repos {
        let repo_res = client
            .post(format!("http://localhost:{}/v1/repos", port))
            .json(&serde_json::json!({
                "name": format!("conc_test_repo_{}", uuid::Uuid::new_v4().simple()),
                "description": "Repo for concurrency test"
            }))
            .send()
            .await
            .expect("Failed to send create repo request");
            
        assert!(repo_res.status().is_success());
        let repo_json: serde_json::Value = repo_res.json().await.unwrap();
        repo_ids.push(repo_json["id"].as_str().unwrap().to_string());
    }

    let agent_id = format!("agent_conc_route_{}", uuid::Uuid::new_v4().simple());
    let scope = "global";

    let num_tasks_per_repo = 5;
    let total_submissions = num_repos * num_tasks_per_repo;
    let mut submit_handles = vec![];
    let success_count = Arc::new(AtomicUsize::new(0));
    // We'll store the first proposal ID for each repo to promote later
    let first_proposals = Arc::new(tokio::sync::Mutex::new(Vec::new()));

    // 2. Concurrently submit proposals across all repos
    for (repo_idx, r_id) in repo_ids.iter().enumerate() {
        for i in 0..num_tasks_per_repo {
            let client_clone = client.clone();
            let r_id_clone = r_id.clone();
            let a_id = format!("{}_agent_{}", agent_id, i);
            let succ_clone = success_count.clone();
            let server_port = port;
            let first_props_clone = first_proposals.clone();

            submit_handles.push(tokio::spawn(async move {
                let res = client_clone
                    .post(format!("http://localhost:{}/v1/repos/{}/proposals", server_port, r_id_clone))
                    .json(&serde_json::json!({
                        "agent_id": a_id,
                        "task_id": format!("task_{}_{}", repo_idx, i),
                        "proposal_title": "Conc Test",
                        "proposal_description": "Testing",
                        "confidence_score": 0.9,
                        "affected_paths": [],
                        "dependencies": [],
                        "requires_tests": false,
                        "requires_security_scan": false,
                        "source": "conc_test",
                        "change_content": format!("def test_{}_{}(): pass\n", repo_idx, i)
                    }))
                    .send()
                    .await;
                    
                if let Ok(r) = res {
                    if r.status().is_success() {
                        succ_clone.fetch_add(1, Ordering::SeqCst);
                        if i == 0 {
                            let body: serde_json::Value = r.json().await.unwrap();
                            let pid = body["proposal_id"].as_str().unwrap().to_string();
                            first_props_clone.lock().await.push((r_id_clone, pid));
                        }
                    } else {
                        println!("Submit failed with status: {}", r.status());
                        let text = r.text().await.unwrap_or_default();
                        println!("Error body: {}", text);
                    }
                } else if let Err(e) = res {
                    println!("Submit error: {}", e);
                }
            }));
        }
    }

    // Wait for all submissions to finish
    for handle in submit_handles {
        let _ = handle.await;
    }

    let actual_successes = success_count.load(Ordering::SeqCst);
    println!("Successful proposal submissions: {}/{}", actual_successes, total_submissions);
    assert_eq!(actual_successes, total_submissions, "Not all submit requests succeeded!");

    // 3. Concurrently request 20 promotions (1 per repo to guarantee no 409s)
    let mut promo_handles = vec![];
    let to_promote = first_proposals.lock().await.clone();
    
    // reset success count for promotions
    success_count.store(0, Ordering::SeqCst);
    
    let num_promos = to_promote.len();
    for (r_id, p_id) in to_promote {
        let client_clone = client.clone();
        let server_port = port;
        let succ_clone = success_count.clone();

        promo_handles.push(tokio::spawn(async move {
            let res = client_clone
                .post(format!("http://localhost:{}/v1/repos/{}/promotions", server_port, r_id))
                .json(&serde_json::json!({
                    "proposal_ids": [p_id],
                    "force": true
                }))
                .send()
                .await;
                
            if let Ok(r) = res {
                if r.status().is_success() {
                    succ_clone.fetch_add(1, Ordering::SeqCst);
                } else {
                    println!("Promo failed with status: {}", r.status());
                    let text = r.text().await.unwrap_or_default();
                    println!("Error body: {}", text);
                }
            } else if let Err(e) = res {
                println!("Promo error: {}", e);
            }
        }));
    }

    for handle in promo_handles {
        let _ = handle.await;
    }
    
    let promo_successes = success_count.load(Ordering::SeqCst);
    println!("Successful promotion requests: {}/{}", promo_successes, num_promos);
    
    // We strictly assert all promos succeed since they are non-conflicting across 20 repos!
    assert_eq!(promo_successes, num_promos, "Not all promotions succeeded under contention!");

    // Tiny wait for the tokio::spawn metric upserts to finish (they run sequentially post-commit)
    // Even though HTTP 201 is returned after trust upsert, they are async match blocks in DB, so it's instantaneous.
    tokio::time::sleep(Duration::from_millis(2000)).await;

    // Kill the server
    let _ = server_process.kill().await;

    // 4. Verify exactly conserved counters
    let prefix = format!("{}%", agent_id);
    let metric = sqlx::query(
        "SELECT COALESCE(SUM(total_proposals), 0)::BIGINT as total_proposals, COALESCE(SUM(promoted_count), 0)::BIGINT as promoted_count FROM aether_agent_metrics WHERE agent_id LIKE $1 AND scope = $2",
    )
    .bind(&prefix)
    .bind(scope)
    .fetch_one(&pool)
    .await
    .expect("Failed to fetch final metric");

    let total_proposals: i64 = metric.get("total_proposals");
    let promoted_count: i64 = metric.get("promoted_count");
    
    println!("Final metrics -> total_proposals: {}, promoted_count: {}", total_proposals, promoted_count);
    
    assert_eq!(
        total_proposals, 
        total_submissions as i64, 
        "Trust metric dropped submit upserts under connection pool contention!"
    );
    
    assert_eq!(
        promoted_count,
        promo_successes as i64,
        "Trust metric dropped promotion upserts under connection pool contention!"
    );
}

#[cfg(feature = "db-postgres")]
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn test_pg_crdt_timing_telemetry_route() {
    use reqwest::Client;
    use sqlx::postgres::PgPoolOptions;
    use sqlx::Row;
    use std::time::Duration;
    use tokio::process::Command;

    let db_url = std::env::var("DATABASE_URL").expect("DATABASE_URL must be set to run pg tests");

    let pool = PgPoolOptions::new()
        .max_connections(5)
        .connect(&db_url)
        .await
        .expect("Failed to connect to PG DB. Check your DATABASE_URL.");

    // Pick a guaranteed free port using TcpListener binding
    let port = {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("Failed to bind TcpListener to find free port");
        listener.local_addr().expect("Failed to get local address").port()
    };
    
    let current_dir = std::env::current_dir().unwrap();
    let mut binary_path = current_dir.join("target/debug/server");
    if !binary_path.exists() {
        binary_path = current_dir.join("../../target/debug/server");
    }
    let mut server_process = Command::new(binary_path)
        .env("DATABASE_URL", &db_url)
        .env("PORT", port.to_string())
        .spawn()
        .expect("Failed to spawn server process");

    // Wait for the server to be ready
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

    // 1. Create a Repository
    let repo_res = client
        .post(format!("http://localhost:{}/v1/repos", port))
        .json(&serde_json::json!({
            "name": format!("crdt_telemetry_repo_{}", uuid::Uuid::new_v4().simple()),
            "description": "Repo for CRDT telemetry E2E test"
        }))
        .send()
        .await
        .expect("Failed to send create repo request");
        
    assert!(repo_res.status().is_success());
    let repo_json: serde_json::Value = repo_res.json().await.unwrap();
    let repo_id = repo_json["id"].as_str().unwrap().to_string();

    // 2. Enable CRDT on the repo directly in the DB
    sqlx::query("UPDATE aether_repositories SET crdt_enabled = true WHERE id = $1")
        .bind(&repo_id)
        .execute(&pool)
        .await
        .expect("Failed to set crdt_enabled to true in DB");

    // 3. Submit a pending proposal
    let agent_id = format!("agent_telemetry_{}", uuid::Uuid::new_v4().simple());
    let submit_res = client
        .post(format!("http://localhost:{}/v1/repos/{}/proposals", port, repo_id))
        .json(&serde_json::json!({
            "agent_id": agent_id,
            "task_id": "task_telemetry_1",
            "proposal_title": "Telemetry Test",
            "proposal_description": "Testing telemetry collection",
            "confidence_score": 0.85,
            "affected_paths": ["src/main.py"],
            "dependencies": [],
            "requires_tests": false,
            "requires_security_scan": false,
            "source": "crdt_telemetry_test",
            "change_content": "def main(): print('Hello timing!')\n"
        }))
        .send()
        .await
        .expect("Failed to submit proposal");

    assert!(submit_res.status().is_success());
    let submit_json: serde_json::Value = submit_res.json().await.unwrap();
    let prop_id = submit_json["proposal_id"].as_str().unwrap().to_string();

    // 4. Request promotion to trigger the CRDT advisory-lock path
    let promo_res = client
        .post(format!("http://localhost:{}/v1/repos/{}/promotions", port, repo_id))
        .json(&serde_json::json!({
            "proposal_ids": [prop_id],
            "force": true
        }))
        .send()
        .await
        .expect("Failed to request promotion");

    let status = promo_res.status();
    let promo_body: serde_json::Value = promo_res.json().await.expect("Failed to parse promotion response JSON");
    
    // Kill the server now to clean up
    let _ = server_process.kill().await;

    assert_eq!(status, reqwest::StatusCode::OK, "Promotion failed with status: {}, body: {:?}", status, promo_body);
    assert_eq!(promo_body["success"].as_bool(), Some(true));

    let prom_id = promo_body["promotion_id"].as_str().unwrap().to_string();
    let api_evidence = promo_body["evidence"].as_object().expect("evidence is not an object");
    let timings_str = api_evidence.get("_timings_ms").expect("Missing _timings_ms in API evidence").as_str().unwrap();
    let timings_json: serde_json::Value = serde_json::from_str(timings_str).expect("Failed to parse _timings_ms JSON");

    println!("HTTP Response Timings:\n{}", serde_json::to_string_pretty(&timings_json).unwrap());

    // Assert all required timing keys exist and are positive
    let required_keys = [
        "t_lock_wait",
        "t_lock_hold",
        "t_total_inner",
        "t_db_commit",
        "t_db_tx_start",
        "t_db_revalidate",
        "t_db_insert_snapshot_gates",
        "t_db_update_proposals_trust",
        "t_db_insert_promotions_heads_cas",
        "t_db_read_pre_orch",
        "t_crdt_eval",
        "t_materialize",
        "t_gates",
        "t_trust_math",
        "t_fs_rename"
    ];

    for key in &required_keys {
        let val = timings_json.get(*key).unwrap_or_else(|| panic!("Missing key '{}' in API timings", key));
        assert!(val.as_f64().unwrap() >= 0.0, "Key '{}' should have a non-negative duration, but got {:?}", key, val);
    }

    // 5. Query the database `aether_promotions` table and assert the exact same keys exist in the `evidence` column
    let db_row = sqlx::query("SELECT evidence FROM aether_promotions WHERE id = $1")
        .bind(&prom_id)
        .fetch_one(&pool)
        .await
        .expect("Failed to fetch promotion from DB");
    
    let db_evidence_str: String = db_row.get("evidence");
    let db_evidence_json: serde_json::Value = serde_json::from_str(&db_evidence_str).expect("Failed to parse DB evidence column JSON");
    let db_timings_str = db_evidence_json.get("_timings_ms").expect("Missing _timings_ms in DB evidence").as_str().unwrap();
    let db_timings_json: serde_json::Value = serde_json::from_str(db_timings_str).expect("Failed to parse DB _timings_ms JSON");

    println!("DB Stored Timings:\n{}", serde_json::to_string_pretty(&db_timings_json).unwrap());

    for key in &required_keys {
        let val = db_timings_json.get(*key).unwrap_or_else(|| panic!("Missing key '{}' in DB timings", key));
        assert!(val.as_f64().unwrap() >= 0.0, "Key '{}' in DB should have a non-negative duration, but got {:?}", key, val);
    }
}

#[cfg(feature = "db-postgres")]
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn test_pg_crdt_concurrency_advisory_lock() {
    use reqwest::Client;
    use sqlx::postgres::PgPoolOptions;
    use sqlx::Row;
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

    // Pick a guaranteed free port using TcpListener binding
    let port = {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("Failed to bind TcpListener to find free port");
        listener.local_addr().expect("Failed to get local address").port()
    };
    
    // Spawn the server using cargo run
    let current_dir = std::env::current_dir().unwrap();
    let mut binary_path = current_dir.join("target/debug/server");
    if !binary_path.exists() {
        binary_path = current_dir.join("../../target/debug/server");
    }
    let mut server_process = Command::new(binary_path)
        .env("DATABASE_URL", &db_url)
        .env("PORT", port.to_string())
        .spawn()
        .expect("Failed to spawn server process");

    // Wait for the server to be ready
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

    // 1. Create a Repository
    let repo_res = client
        .post(format!("http://localhost:{}/v1/repos", port))
        .json(&serde_json::json!({
            "name": format!("crdt_conc_repo_{}", uuid::Uuid::new_v4().simple()),
            "description": "Repo for CRDT concurrency E2E test"
        }))
        .send()
        .await
        .expect("Failed to send create repo request");
        
    assert!(repo_res.status().is_success());
    let repo_json: serde_json::Value = repo_res.json().await.unwrap();
    let repo_id = repo_json["id"].as_str().unwrap().to_string();

    // 2. Enable CRDT on the repo directly in the DB
    sqlx::query("UPDATE aether_repositories SET crdt_enabled = true WHERE id = $1")
        .bind(&repo_id)
        .execute(&pool)
        .await
        .expect("Failed to set crdt_enabled to true in DB");

    // 3. Concurrently submit 10 proposals from different agents targeting this SAME repository
    let num_proposals = 10;
    let mut submit_handles = vec![];
    let proposals = Arc::new(tokio::sync::Mutex::new(Vec::new()));
    
    for i in 0..num_proposals {
        let client_clone = client.clone();
        let repo_id_clone = repo_id.clone();
        let proposals_clone = proposals.clone();
        let server_port = port;

        submit_handles.push(tokio::spawn(async move {
            let agent_id = format!("agent_crdt_conc_{}", i);
            let res = client_clone
                .post(format!("http://localhost:{}/v1/repos/{}/proposals", server_port, repo_id_clone))
                .json(&serde_json::json!({
                    "agent_id": agent_id,
                    "task_id": format!("task_crdt_conc_{}", i),
                    "proposal_title": format!("CRDT Concurrency Test {}", i),
                    "proposal_description": "Testing telemetry collection under pressure",
                    "confidence_score": 0.85,
                    "affected_paths": ["src/main.py"],
                    "dependencies": [],
                    "requires_tests": false,
                    "requires_security_scan": false,
                    "source": "crdt_concurrency_test",
                    "change_content": format!("def fn_{}(): pass\n", i)
                }))
                .send()
                .await;
                
            if let Ok(r) = res {
                if r.status().is_success() {
                    let body: serde_json::Value = r.json().await.unwrap();
                    let pid = body["proposal_id"].as_str().unwrap().to_string();
                    proposals_clone.lock().await.push(pid);
                } else {
                    panic!("Proposal submission {} failed: {}", i, r.status());
                }
            } else {
                panic!("Proposal submission {} network error", i);
            }
        }));
    }

    for handle in submit_handles {
        let _ = handle.await;
    }

    let proposal_ids = proposals.lock().await.clone();
    assert_eq!(proposal_ids.len(), num_proposals, "Not all concurrent proposals were successfully submitted!");

    // 4. Concurrently request promotion for all 10 proposals targeting the same repository
    // Under OCC model, concurrent requests may fail with 409 Conflict and need client-side retry.
    let mut promo_handles = vec![];
    let success_count = Arc::new(AtomicUsize::new(0));
    let promotion_ids = Arc::new(tokio::sync::Mutex::new(Vec::new()));

    for p_id in proposal_ids {
        let client_clone = client.clone();
        let repo_id_clone = repo_id.clone();
        let server_port = port;
        let succ_clone = success_count.clone();
        let p_ids_clone = promotion_ids.clone();

        promo_handles.push(tokio::spawn(async move {
            let mut attempts = 0;
            loop {
                let res = client_clone
                    .post(format!("http://localhost:{}/v1/repos/{}/promotions", server_port, repo_id_clone))
                    .json(&serde_json::json!({
                        "proposal_ids": [p_id],
                        "force": true
                    }))
                    .send()
                    .await;
                    
                match res {
                    Ok(r) => {
                        let status = r.status();
                        let body: serde_json::Value = r.json().await.unwrap();
                        if status.is_success() && body["success"].as_bool() == Some(true) {
                            succ_clone.fetch_add(1, Ordering::SeqCst);
                            let prom_id = body["promotion_id"].as_str().unwrap().to_string();
                            p_ids_clone.lock().await.push((prom_id, body));
                            break;
                        } else if status == reqwest::StatusCode::CONFLICT {
                            attempts += 1;
                            if attempts > 50 {
                                println!("Promo failed after max retries: status: {}, body: {:?}", status, body);
                                break;
                            }
                            let sleep_ms = 40 + (attempts * 10) + (u64::from(p_id.as_bytes()[p_id.len()-1]) % 30);
                            tokio::time::sleep(Duration::from_millis(sleep_ms)).await;
                        } else {
                            println!("Promo failed under concurrent pressure. status: {}, body: {:?}", status, body);
                            break;
                        }
                    }
                    Err(e) => {
                        println!("Promo network error under concurrent pressure: {}", e);
                        break;
                    }
                }
            }
        }));
    }

    for handle in promo_handles {
        let _ = handle.await;
    }

    let total_promo_successes = success_count.load(Ordering::SeqCst);
    println!("CRDT Concurrency Promotion Successes: {}/{}", total_promo_successes, num_proposals);

    // Assert that ALL 10 concurrent promotions eventually succeeded after client-side retries!
    assert_eq!(
        total_promo_successes, 
        num_proposals, 
        "CRDT OCC model failed to commit all concurrent promotions after retries!"
    );

    // Tiny sleep to ensure post-commit DB updates are finished
    tokio::time::sleep(Duration::from_millis(150)).await;

    // 5. Verify the evidence column inside the DB for all these promotions
    // We check that at least some of them queued successfully (t_lock_wait > 0)
    let committed_promos = promotion_ids.lock().await.clone();
    let mut some_queued = false;

    for (prom_id, api_body) in committed_promos {
        // A. Verify timings in API response
        let api_evidence = api_body["evidence"].as_object().expect("API evidence is not an object");
        let timings_str = api_evidence.get("_timings_ms").expect("Missing _timings_ms in API evidence").as_str().unwrap();
        let _timings_json: serde_json::Value = serde_json::from_str(timings_str).expect("Failed to parse API timings JSON");

        // B. Verify timings in Database promotions table
        let db_row = sqlx::query("SELECT evidence FROM aether_promotions WHERE id = $1")
            .bind(&prom_id)
            .fetch_one(&pool)
            .await
            .expect("Failed to fetch promotion from DB");
        
        let db_evidence_str: String = db_row.get("evidence");
        let db_evidence_json: serde_json::Value = serde_json::from_str(&db_evidence_str).expect("Failed to parse DB evidence column JSON");
        let db_timings_str = db_evidence_json.get("_timings_ms").expect("Missing _timings_ms in DB evidence").as_str().unwrap();
        let db_timings_json: serde_json::Value = serde_json::from_str(db_timings_str).expect("Failed to parse DB _timings_ms JSON");

        let required_keys = [
            "t_lock_wait",
            "t_lock_hold",
            "t_total_inner",
            "t_db_commit",
            "t_db_tx_start",
            "t_db_revalidate",
            "t_db_insert_snapshot_gates",
            "t_db_update_proposals_trust",
            "t_db_insert_promotions_heads_cas",
            "t_db_read_pre_orch",
            "t_crdt_eval",
            "t_materialize",
            "t_gates",
            "t_trust_math",
            "t_fs_rename"
        ];

        for key in &required_keys {
            let val = db_timings_json.get(*key).unwrap_or_else(|| panic!("Missing key '{}' in DB timings", key));
            assert!(val.as_f64().unwrap() >= 0.0, "Key '{}' should be non-negative", key);
        }

        // C. Lock Queueing Verification: verify wait times are logged, and check if wait time > 0 (meaning it queued behind others)
        let wait_val = db_timings_json.get("t_lock_wait").unwrap().as_f64().unwrap();
        if wait_val > 0.0 {
            some_queued = true;
        }
    }

    // Kill the server process
    let _ = server_process.kill().await;

    // Assert that we successfully verified queueing (at least one promotion waited for the advisory lock!)
    assert!(
        some_queued, 
        "Failed to verify advisory-lock queueing behavior: no concurrent requests queued (all t_lock_wait were 0)"
    );
}

#[cfg(feature = "db-postgres")]
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn test_pg_crdt_frontier_linearization() {
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

    // Pick a guaranteed free port using TcpListener binding
    let port = {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("Failed to bind TcpListener to find free port");
        listener.local_addr().expect("Failed to get local address").port()
    };
    
    // Spawn server using cargo run
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

    // Wait for server to be ready
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

    // 1. Create a Repository
    let repo_res = client
        .post(format!("http://localhost:{}/v1/repos", port))
        .json(&serde_json::json!({
            "name": format!("crdt_test_repo_{}", uuid::Uuid::new_v4().simple()),
            "description": "Repo for CRDT E2E test"
        }))
        .send()
        .await
        .expect("Failed to create repo");
    assert!(repo_res.status().is_success());
    let repo_json: serde_json::Value = repo_res.json().await.unwrap();
    let repo_id = repo_json["id"].as_str().unwrap().to_string();

    // 2. Enable CRDT on this repository directly in the DB
    sqlx::query("UPDATE aether_repositories SET crdt_enabled = true WHERE id = $1")
        .bind(&repo_id)
        .execute(&pool)
        .await
        .expect("Failed to enable crdt on repo");

    // 3. Submit initial proposal A from agent_a (Trust 0.8)
    let submit_a_res = client
        .post(format!("http://localhost:{}/v1/repos/{}/proposals", port, &repo_id))
        .json(&serde_json::json!({
            "agent_id": "agent_a",
            "task_id": "task_a_1",
            "proposal_title": "Initial Proposal A",
            "proposal_description": "Initial Proposal A",
            "confidence_score": 0.8,
            "affected_paths": ["src/main.py"],
            "dependencies": [],
            "requires_tests": false,
            "requires_security_scan": false,
            "source": "frontier_test",
            "change_type": "full_file",
            "change_content": "x = 42\n"
        }))
        .send()
        .await
        .expect("Failed to submit proposal A");
    assert!(submit_a_res.status().is_success());
    let submit_a_json: serde_json::Value = submit_a_res.json().await.unwrap();
    let prop_a_id = submit_a_json["proposal_id"].as_str().unwrap().to_string();

    // Promote Proposal A to create genesis head
    let promote_a_res = client
        .post(format!("http://localhost:{}/v1/repos/{}/promotions", port, &repo_id))
        .json(&serde_json::json!({
            "proposal_ids": [prop_a_id],
            "force": true
        }))
        .send()
        .await
        .expect("Failed to promote A");
    assert!(promote_a_res.status().is_success());
    let promote_a_json: serde_json::Value = promote_a_res.json().await.unwrap();
    let head_a_id = promote_a_json["new_head_id"].as_str().unwrap().to_string();

    // Submit concurrent proposal B from agent_b (Trust 0.75) based on Head A
    let submit_b_res = client
        .post(format!("http://localhost:{}/v1/repos/{}/proposals", port, &repo_id))
        .json(&serde_json::json!({
            "agent_id": "agent_b",
            "task_id": "task_b_1",
            "proposal_title": "Proposal B",
            "proposal_description": "Proposal B",
            "base_head_id": head_a_id,
            "confidence_score": 0.75,
            "affected_paths": ["src/main.py"],
            "dependencies": [],
            "requires_tests": false,
            "requires_security_scan": false,
            "source": "frontier_test",
            "change_type": "full_file",
            "change_content": "x = 42\ny = 100\n"
        }))
        .send()
        .await
        .expect("Failed to submit proposal B");
    assert!(submit_b_res.status().is_success());
    let prop_b_id = submit_b_res.json::<serde_json::Value>().await.unwrap()["proposal_id"].as_str().unwrap().to_string();

    // Submit concurrent proposal C from agent_c (Trust 0.90) based on Head A
    let submit_c_res = client
        .post(format!("http://localhost:{}/v1/repos/{}/proposals", port, &repo_id))
        .json(&serde_json::json!({
            "agent_id": "agent_c",
            "task_id": "task_c_1",
            "proposal_title": "Proposal C",
            "proposal_description": "Proposal C",
            "base_head_id": head_a_id,
            "confidence_score": 0.90, // Higher trust than B
            "affected_paths": ["src/main.py"],
            "dependencies": [],
            "requires_tests": false,
            "requires_security_scan": false,
            "source": "frontier_test",
            "change_type": "full_file",
            "change_content": "x = 42\nz = 200\n"
        }))
        .send()
        .await
        .expect("Failed to submit proposal C");
    assert!(submit_c_res.status().is_success());
    let prop_c_id = submit_c_res.json::<serde_json::Value>().await.unwrap()["proposal_id"].as_str().unwrap().to_string();

    // Promote B and C concurrently to cause a causal fork (frontier width > 1)
    let promote_b_res = client
        .post(format!("http://localhost:{}/v1/repos/{}/promotions", port, &repo_id))
        .json(&serde_json::json!({
            "proposal_ids": [prop_b_id],
            "force": true
        }))
        .send()
        .await
        .expect("Failed to promote B");
    assert!(promote_b_res.status().is_success());
    let _head_b_id = promote_b_res.json::<serde_json::Value>().await.unwrap()["new_head_id"].as_str().unwrap().to_string();

    let promote_c_res = client
        .post(format!("http://localhost:{}/v1/repos/{}/promotions", port, &repo_id))
        .json(&serde_json::json!({
            "proposal_ids": [prop_c_id],
            "force": true
        }))
        .send()
        .await
        .expect("Failed to promote C");
    assert!(promote_c_res.status().is_success());
    let head_c_id = promote_c_res.json::<serde_json::Value>().await.unwrap()["new_head_id"].as_str().unwrap().to_string();

    // 5. Query active frontier endpoint and verify width and arbitration!
    let frontier_res = client
        .get(format!("http://localhost:{}/v1/repos/{}/frontier", port, &repo_id))
        .send()
        .await
        .expect("Failed to fetch frontier");
    assert!(frontier_res.status().is_success());
    let frontier_json: serde_json::Value = frontier_res.json().await.unwrap();
    
    let width = frontier_json["width"].as_u64().unwrap();
    let arbitrated_snap = frontier_json["arbitrated_snapshot_id"].as_str().unwrap().to_string();
    println!("Frontier width: {}, arbitrated_snapshot: {}", width, arbitrated_snap);
    
    // We expect the frontier to contain both concurrent promotion heads (head_b_id and head_c_id)
    assert_eq!(width, 2, "Causal frontier should represent both concurrent forks!");

    // Query standard linear /head endpoint and assert it returns the deterministically arbitrated winner!
    let head_res = client
        .get(format!("http://localhost:{}/v1/repos/{}/head", port, &repo_id))
        .send()
        .await
        .expect("Failed to fetch head");
    assert!(head_res.status().is_success());
    let head_json: serde_json::Value = head_res.json().await.unwrap();
    let current_head_id = head_json["head_id"].as_str().unwrap().to_string();
    let current_snap_id = head_json["snapshot_id"].as_str().unwrap().to_string();

    // Since agent_c (Trust 0.90) has higher trust than agent_b (Trust 0.75), C must win arbitration!
    assert_eq!(current_head_id, head_c_id, "Projected head must be the arbitrated winner C!");
    assert!(current_snap_id == arbitrated_snap || current_snap_id.starts_with("projected_"), "Projected snapshot must either match arbitrated snapshot or be a projected merge snapshot!");

    // Verify Prerequisite 4: Projected-Merge Content Verification (Strict)
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
    let materialized_path = std::path::Path::new(&home)
        .join(".aether/aether_storage")
        .join(&repo_id)
        .join("snapshots")
        .join(&current_snap_id)
        .join("src/main.py");
    assert!(materialized_path.exists(), "Materialized projected snapshot file main.py must exist at {:?}", materialized_path);
    let content = std::fs::read_to_string(&materialized_path).expect("Failed to read projected snapshot file content!");
    println!("DEBUG frontier content = {:?}", content);
    assert!(content.contains("y = 100"), "Projected snapshot must contain y = 100 from proposal B!");
    assert!(content.contains("z = 200"), "Projected snapshot must contain z = 200 from proposal C!");
    println!("✅ Projected merge content verified strictly!");

    // Kill the server
    let _ = server_process.kill().await;
}

#[cfg(feature = "db-postgres")]
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn test_pg_token_mode_e2e() {
    use reqwest::Client;
    use sqlx::postgres::PgPoolOptions;
    use std::time::Duration;
    use tokio::process::Command;

    let db_url = std::env::var("DATABASE_URL").expect("DATABASE_URL must be set to run pg tests");

    let pool = PgPoolOptions::new()
        .max_connections(5)
        .connect(&db_url)
        .await
        .expect("Failed to connect to PG DB.");

    // Pick a guaranteed free port using TcpListener binding
    let port = {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("Failed to bind TcpListener to find free port");
        listener.local_addr().expect("Failed to get local address").port()
    };
    
    let current_dir = std::env::current_dir().unwrap();
    let mut binary_path = current_dir.join("target/debug/server");
    if !binary_path.exists() {
        binary_path = current_dir.join("../../target/debug/server");
    }

    let mut server_process = Command::new(binary_path)
        .env("DATABASE_URL", &db_url)
        .env("PORT", port.to_string())
        .env("AETHER_CRDT_MODE", "token")
        .spawn()
        .expect("Failed to spawn server process");

    // Wait for the server to be ready
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

    // 1. Create a repository
    let repo_res = client
        .post(format!("http://localhost:{}/v1/repos", port))
        .json(&serde_json::json!({
            "name": format!("token-mode-repo-{}", uuid::Uuid::new_v4().simple()),
            "description": "Token-level CRDT validation"
        }))
        .send()
        .await
        .unwrap();
    assert!(repo_res.status().is_success());
    let repo_id = repo_res.json::<serde_json::Value>().await.unwrap()["id"].as_str().unwrap().to_string();

    // 1.5 Enable CRDT on this repository directly in the DB
    sqlx::query("UPDATE aether_repositories SET crdt_enabled = true WHERE id = $1")
        .bind(&repo_id)
        .execute(&pool)
        .await
        .expect("Failed to enable crdt on repo");

    // 2. Submit initial proposal
    let submit_1_res = client
        .post(format!("http://localhost:{}/v1/repos/{}/proposals", port, &repo_id))
        .json(&serde_json::json!({
            "agent_id": "token_agent_1",
            "task_id": "task_1",
            "base_head_id": null,
            "confidence_score": 0.95,
            "affected_paths": ["src/main.py"],
            "change_type": "full_file",
            "change_content": "def hello():\n    print(\"Hello Token Mode\")\n"
        }))
        .send()
        .await
        .unwrap();
    assert!(submit_1_res.status().is_success());
    let prop_1_id = submit_1_res.json::<serde_json::Value>().await.unwrap()["proposal_id"].as_str().unwrap().to_string();

    // 3. Promote initial proposal to establish genesis
    let promote_1_res = client
        .post(format!("http://localhost:{}/v1/repos/{}/promotions", port, &repo_id))
        .json(&serde_json::json!({
            "proposal_ids": [prop_1_id],
            "force": true
        }))
        .send()
        .await
        .unwrap();
    assert!(promote_1_res.status().is_success());
    let head_1_id = promote_1_res.json::<serde_json::Value>().await.unwrap()["new_head_id"].as_str().unwrap().to_string();

    // 4. Submit concurrent proposal B based on head_1_id
    let submit_b_res = client
        .post(format!("http://localhost:{}/v1/repos/{}/proposals", port, &repo_id))
        .json(&serde_json::json!({
            "agent_id": "token_agent_b",
            "task_id": "task_b",
            "base_head_id": head_1_id,
            "confidence_score": 0.75,
            "affected_paths": ["src/main.py"],
            "change_type": "full_file",
            "change_content": "def hello():\n    print(\"Hello Token Mode\")\n    x = 100\n"
        }))
        .send()
        .await
        .unwrap();
    assert!(submit_b_res.status().is_success());
    let prop_b_id = submit_b_res.json::<serde_json::Value>().await.unwrap()["proposal_id"].as_str().unwrap().to_string();

    // 5. Submit concurrent proposal C based on head_1_id
    let submit_c_res = client
        .post(format!("http://localhost:{}/v1/repos/{}/proposals", port, &repo_id))
        .json(&serde_json::json!({
            "agent_id": "token_agent_c",
            "task_id": "task_c",
            "base_head_id": head_1_id,
            "confidence_score": 0.90, // Higher confidence
            "affected_paths": ["src/main.py"],
            "change_type": "full_file",
            "change_content": "def hello():\n    print(\"Hello Token Mode\")\n    y = 200\n"
        }))
        .send()
        .await
        .unwrap();
    assert!(submit_c_res.status().is_success());
    let prop_c_id = submit_c_res.json::<serde_json::Value>().await.unwrap()["proposal_id"].as_str().unwrap().to_string();

    // 6. Promote B and C concurrently to cause causal fork
    let promote_b_res = client
        .post(format!("http://localhost:{}/v1/repos/{}/promotions", port, &repo_id))
        .json(&serde_json::json!({
            "proposal_ids": [prop_b_id],
            "force": true
        }))
        .send()
        .await
        .unwrap();
    assert!(promote_b_res.status().is_success());

    let promote_c_res = client
        .post(format!("http://localhost:{}/v1/repos/{}/promotions", port, &repo_id))
        .json(&serde_json::json!({
            "proposal_ids": [prop_c_id],
            "force": true
        }))
        .send()
        .await
        .unwrap();
    assert!(promote_c_res.status().is_success());

    // 7. Verify causal frontier width
    let frontier_res = client
        .get(format!("http://localhost:{}/v1/repos/{}/frontier", port, &repo_id))
        .send()
        .await
        .unwrap();
    assert!(frontier_res.status().is_success());
    let frontier_json: serde_json::Value = frontier_res.json().await.unwrap();
    assert_eq!(frontier_json["width"].as_u64().unwrap(), 2);

    // 8. Query head and verify token-level CRDT merged snapshot
    let head_res = client
        .get(format!("http://localhost:{}/v1/repos/{}/head", port, &repo_id))
        .send()
        .await
        .unwrap();
    assert!(head_res.status().is_success());
    
    let head_json: serde_json::Value = head_res.json().await.unwrap();
    let current_snap_id = head_json["snapshot_id"].as_str().unwrap().to_string();
    assert!(current_snap_id.starts_with("projected_"));

    // 9. Strictly verify projected-merge content for token mode
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
    let materialized_path = std::path::Path::new(&home)
        .join(".aether/aether_storage")
        .join(&repo_id)
        .join("snapshots")
        .join(&current_snap_id)
        .join("src/main.py");
    assert!(materialized_path.exists(), "Materialized token-mode projected snapshot file main.py must exist at {:?}", materialized_path);
    let content = std::fs::read_to_string(&materialized_path).expect("Failed to read projected snapshot file content!");
    println!("DEBUG token content = {:?}", content);
    assert!(content.contains("x = 100"), "Projected snapshot must contain x = 100 from proposal B!");
    assert!(content.contains("y = 200"), "Projected snapshot must contain y = 200 from proposal C!");
    println!("✅ Token-mode projected merge content verified strictly!");

    let _ = server_process.kill().await;
}

#[cfg(feature = "db-postgres")]
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn test_pg_task_local_timing_isolation() {
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::sync::Arc;
    use tokio::time::{sleep, Duration};

    let barrier = Arc::new(tokio::sync::Barrier::new(5));
    let mut handles = vec![];

    for i in 1..=5 {
        let b = barrier.clone();
        handles.push(tokio::spawn(async move {
            // Scope task-local RGA_ALIGNMENT_TIME_NS
            crdt::rga::RGA_ALIGNMENT_TIME_NS.scope(
                AtomicU64::new(0),
                async move {
                    // Stage 1: Add a task-specific duration
                    crdt::rga::RGA_ALIGNMENT_TIME_NS.try_with(|cell| {
                        cell.fetch_add(i * 1000, Ordering::SeqCst);
                    }).unwrap();

                    // Wait for all tasks to reach this point (forcing yield & thread-level multiplexing)
                    b.wait().await;

                    // Stage 2: Sleep to allow interleaved execution
                    sleep(Duration::from_millis(10 + i * 5)).await;

                    // Stage 3: Add more duration
                    crdt::rga::RGA_ALIGNMENT_TIME_NS.try_with(|cell| {
                        cell.fetch_add(i * 500, Ordering::SeqCst);
                    }).unwrap();

                    // Retrieve final value
                    let val = crdt::rga::RGA_ALIGNMENT_TIME_NS.try_with(|cell| {
                        cell.load(Ordering::SeqCst)
                    }).unwrap();

                    // Check that the value is exactly i * 1000 + i * 500
                    assert_eq!(val, i * 1500, "Task-local value was contaminated under concurrency!");
                }
            ).await;
        }));
    }

    for h in handles {
        h.await.unwrap();
    }
    println!("✅ Task-local concurrency isolation validated successfully!");
}

#[cfg(feature = "db-postgres")]
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn test_pg_crdt_frontier_width_cap_backpressure() {
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

    // Pick a guaranteed free port using TcpListener binding
    let port = {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("Failed to bind TcpListener to find free port");
        listener.local_addr().expect("Failed to get local address").port()
    };
    
    // Spawn server using cargo run
    let current_dir = std::env::current_dir().unwrap();
    let mut binary_path = current_dir.join("target/debug/server");
    if !binary_path.exists() {
        binary_path = current_dir.join("../../target/debug/server");
    }
    let mut server_process = Command::new(binary_path)
        .env("DATABASE_URL", &db_url)
        .env("PORT", port.to_string())
        .env("AETHER_CRDT_MODE", "frontier")
        .env("AETHER_MAX_FRONTIER_WIDTH", "1") // Set max frontier width to 1!
        .spawn()
        .expect("Failed to spawn server process");

    // Wait for server to be ready
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

    // 1. Create a Repository
    let repo_res = client
        .post(format!("http://localhost:{}/v1/repos", port))
        .json(&serde_json::json!({
            "name": format!("crdt_cap_repo_{}", uuid::Uuid::new_v4().simple()),
            "description": "Repo for CRDT width-cap backpressure E2E test"
        }))
        .send()
        .await
        .expect("Failed to create repo");
    assert!(repo_res.status().is_success());
    let repo_json: serde_json::Value = repo_res.json().await.unwrap();
    let repo_id = repo_json["id"].as_str().unwrap().to_string();

    // 2. Enable CRDT on this repository directly in the DB
    sqlx::query("UPDATE aether_repositories SET crdt_enabled = true WHERE id = $1")
        .bind(&repo_id)
        .execute(&pool)
        .await
        .expect("Failed to enable crdt on repo");

    // 3. Submit initial proposal A from agent_a (Trust 0.8)
    let submit_a_res = client
        .post(format!("http://localhost:{}/v1/repos/{}/proposals", port, &repo_id))
        .json(&serde_json::json!({
            "agent_id": "agent_a",
            "task_id": "task_cap_a",
            "proposal_title": "Initial Proposal A",
            "proposal_description": "Initial Proposal A",
            "confidence_score": 0.8,
            "affected_paths": ["src/main.py"],
            "dependencies": [],
            "requires_tests": false,
            "requires_security_scan": false,
            "source": "frontier_cap_test",
            "change_type": "full_file",
            "change_content": "x = 42\n"
        }))
        .send()
        .await
        .expect("Failed to submit proposal A");
    assert!(submit_a_res.status().is_success());
    let submit_a_json: serde_json::Value = submit_a_res.json().await.unwrap();
    let prop_a_id = submit_a_json["proposal_id"].as_str().unwrap().to_string();

    // Promote Proposal A to create genesis head
    let promote_a_res = client
        .post(format!("http://localhost:{}/v1/repos/{}/promotions", port, &repo_id))
        .json(&serde_json::json!({
            "proposal_ids": [prop_a_id],
            "force": true
        }))
        .send()
        .await
        .expect("Failed to promote A");
    assert!(promote_a_res.status().is_success());
    let promote_a_json: serde_json::Value = promote_a_res.json().await.unwrap();
    let head_a_id = promote_a_json["new_head_id"].as_str().unwrap().to_string();

    // Submit concurrent proposal B based on Head A
    let submit_b_res = client
        .post(format!("http://localhost:{}/v1/repos/{}/proposals", port, &repo_id))
        .json(&serde_json::json!({
            "agent_id": "agent_b",
            "task_id": "task_cap_b",
            "proposal_title": "Proposal B",
            "proposal_description": "Proposal B",
            "base_head_id": head_a_id,
            "confidence_score": 0.75,
            "affected_paths": ["src/main.py"],
            "dependencies": [],
            "requires_tests": false,
            "requires_security_scan": false,
            "source": "frontier_cap_test",
            "change_type": "full_file",
            "change_content": "x = 42\ny = 100\n"
        }))
        .send()
        .await
        .expect("Failed to submit proposal B");
    assert!(submit_b_res.status().is_success());
    let prop_b_id = submit_b_res.json::<serde_json::Value>().await.unwrap()["proposal_id"].as_str().unwrap().to_string();

    // Submit concurrent proposal C based on Head A
    let submit_c_res = client
        .post(format!("http://localhost:{}/v1/repos/{}/proposals", port, &repo_id))
        .json(&serde_json::json!({
            "agent_id": "agent_c",
            "task_id": "task_cap_c",
            "proposal_title": "Proposal C",
            "proposal_description": "Proposal C",
            "base_head_id": head_a_id,
            "confidence_score": 0.90,
            "affected_paths": ["src/main.py"],
            "dependencies": [],
            "requires_tests": false,
            "requires_security_scan": false,
            "source": "frontier_cap_test",
            "change_type": "full_file",
            "change_content": "x = 42\nz = 200\n"
        }))
        .send()
        .await
        .expect("Failed to submit proposal C");
    assert!(submit_c_res.status().is_success());
    let prop_c_id = submit_c_res.json::<serde_json::Value>().await.unwrap()["proposal_id"].as_str().unwrap().to_string();

    // Promote B. This will succeed and update the frontier (width = 1).
    let promote_b_res = client
        .post(format!("http://localhost:{}/v1/repos/{}/promotions", port, &repo_id))
        .json(&serde_json::json!({
            "proposal_ids": [prop_b_id],
            "force": true
        }))
        .send()
        .await
        .expect("Failed to promote B");
    assert!(promote_b_res.status().is_success());

    // Promote C concurrently. Since it's concurrent with B, adding C to the frontier
    // would increase the width to 2, which exceeds AETHER_MAX_FRONTIER_WIDTH=1.
    // This must trigger a 409 Conflict.
    let promote_c_res = client
        .post(format!("http://localhost:{}/v1/repos/{}/promotions", port, &repo_id))
        .json(&serde_json::json!({
            "proposal_ids": [prop_c_id],
            "force": true
        }))
        .send()
        .await
        .expect("Failed to send promotion C request");

    let status = promote_c_res.status();
    let body: serde_json::Value = promote_c_res.json().await.expect("Failed to parse C promotion response");

    // Kill the server
    let _ = server_process.kill().await;

    // Verify 409 status code and FRONTIER_AT_CAPACITY error details
    assert_eq!(status, reqwest::StatusCode::CONFLICT, "Promotion C should fail with 409 Conflict");
    assert_eq!(body["success"].as_bool(), Some(false));
    assert!(body["message"].as_str().unwrap().contains("FRONTIER_AT_CAPACITY"), "Message should contain FRONTIER_AT_CAPACITY");
    
    let evidence = body["evidence"].as_object().expect("evidence is not an object");
    assert_eq!(evidence.get("error_code").unwrap().as_str(), Some("FRONTIER_AT_CAPACITY"));
    println!("✅ Frontier width cap 409 backpressure verified successfully!");
}



