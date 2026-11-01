#[cfg(feature = "db-postgres")]
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn test_admission_valve_throttling_and_clearance() {
    use reqwest::Client;
    use sqlx::postgres::PgPoolOptions;
    use std::time::Duration;
    use tokio::process::Command;

    let db_url = std::env::var("DATABASE_URL").expect("DATABASE_URL must be set to run pg tests");

    let pool = PgPoolOptions::new()
        .max_connections(5)
        .connect(&db_url)
        .await
        .expect("Failed to connect to PG DB. Check your DATABASE_URL.");

    // Clean up tables before starting test
    sqlx::query("TRUNCATE TABLE aether_proposals, aether_promotions, aether_heads, aether_repositories CASCADE;")
        .execute(&pool)
        .await
        .ok();

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
    
    // Spawn server with path active limit set to 2
    let mut server_process = Command::new(binary_path)
        .env("DATABASE_URL", &db_url)
        .env("PORT", port.to_string())
        .env("AETHER_MAX_PATH_ACTIVE_PROPOSALS", "2")
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

    // 1. Create Repository
    let repo_res = client
        .post(format!("http://localhost:{}/v1/repos", port))
        .json(&serde_json::json!({
            "name": "valve_test_repo",
            "description": "Repo for valve test"
        }))
        .send()
        .await
        .expect("Failed to send create repo request");
        
    assert!(repo_res.status().is_success());
    let repo_json: serde_json::Value = repo_res.json().await.unwrap();
    let repo_id = repo_json["id"].as_str().unwrap().to_string();

    // Enable CRDT mode for the test repo so path active checking and conflict behavior are active
    sqlx::query("UPDATE aether_repositories SET crdt_enabled = true WHERE id = $1;")
        .bind(&repo_id)
        .execute(&pool)
        .await
        .expect("Failed to enable CRDT on test repository");

    // Get current head
    let head_res = client
        .get(format!("http://localhost:{}/v1/repos/{}/head", port, repo_id))
        .send()
        .await
        .expect("Failed to get head");
    assert!(head_res.status().is_success());
    let head_json: serde_json::Value = head_res.json().await.unwrap();
    let head_id = head_json["head_id"].as_str().unwrap().to_string();
    let head_seq = head_json["sequence"].as_i64().unwrap();

    // 2. Submit 2 concurrent proposals on the same path "src/main.py"
    // Since max path active is 2, both should succeed.
    let p1_res = client
        .post(format!("http://localhost:{}/v1/repos/{}/proposals", port, repo_id))
        .json(&serde_json::json!({
            "agent_id": "agent_1",
            "task_id": "task_1",
            "proposal_title": "P1",
            "proposal_description": "Testing P1",
            "base_head_id": head_id,
            "base_head_sequence": head_seq,
            "affected_paths": ["src/main.py"],
            "change_type": "full_file",
            "change_content": "def test_p1(): pass\n",
            "confidence_score": 0.9,
            "dependencies": [],
            "requires_tests": false,
            "requires_security_scan": false,
            "source": "conc_test"
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(p1_res.status(), 201);
    let p1_data: serde_json::Value = p1_res.json().await.unwrap();
    let p1_id = p1_data["proposal_id"].as_str().unwrap().to_string();

    let p2_res = client
        .post(format!("http://localhost:{}/v1/repos/{}/proposals", port, repo_id))
        .json(&serde_json::json!({
            "agent_id": "agent_2",
            "task_id": "task_2",
            "proposal_title": "P2",
            "proposal_description": "Testing P2",
            "base_head_id": head_id,
            "base_head_sequence": head_seq,
            "affected_paths": ["src/main.py"],
            "change_type": "full_file",
            "change_content": "def test_p2(): pass\n",
            "confidence_score": 0.9,
            "dependencies": [],
            "requires_tests": false,
            "requires_security_scan": false,
            "source": "conc_test"
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(p2_res.status(), 201);
    let p2_data: serde_json::Value = p2_res.json().await.unwrap();
    let _p2_id = p2_data["proposal_id"].as_str().unwrap().to_string();

    // 3. Submit a 3rd proposal on the same path "src/main.py"
    // Since max path active is 2, this must be blocked with 429 Too Many Requests.
    let p3_res = client
        .post(format!("http://localhost:{}/v1/repos/{}/proposals", port, repo_id))
        .json(&serde_json::json!({
            "agent_id": "agent_3",
            "task_id": "task_3",
            "proposal_title": "P3",
            "proposal_description": "Testing P3",
            "base_head_id": head_id,
            "base_head_sequence": head_seq,
            "affected_paths": ["src/main.py"],
            "change_type": "full_file",
            "change_content": "def test_p3(): pass\n",
            "confidence_score": 0.9,
            "dependencies": [],
            "requires_tests": false,
            "requires_security_scan": false,
            "source": "conc_test"
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(p3_res.status(), 429);

    // 4. Promote P1 to advance the head sequence
    let prom_res = client
        .post(format!("http://localhost:{}/v1/repos/{}/promotions", port, repo_id))
        .json(&serde_json::json!({
            "proposal_ids": [p1_id]
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(prom_res.status(), 200);
    let prom_data: serde_json::Value = prom_res.json().await.unwrap();
    assert_eq!(prom_data["success"].as_bool(), Some(true));

    // 5. Request promotion of P1 again.
    // Assert the server returns 409 Conflict since it has already been processed (no longer pending).
    let prom_again_res = client
        .post(format!("http://localhost:{}/v1/repos/{}/promotions", port, repo_id))
        .json(&serde_json::json!({
            "proposal_ids": [p1_id]
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(prom_again_res.status(), 409);
    let prom_again_data: serde_json::Value = prom_again_res.json().await.unwrap();
    assert_eq!(prom_again_data["success"].as_bool(), Some(false));
    assert!(prom_again_data["message"].as_str().unwrap().contains("No pending proposals"));

    // Get the new head sequence
    let head2_res = client
        .get(format!("http://localhost:{}/v1/repos/{}/head", port, repo_id))
        .send()
        .await
        .unwrap();
    let head2_json: serde_json::Value = head2_res.json().await.unwrap();
    let head2_id = head2_json["head_id"].as_str().unwrap().to_string();
    let head2_seq = head2_json["sequence"].as_i64().unwrap();
    assert_eq!(head2_seq, head_seq + 1);

    // 6. Submit a fresh proposal (P4) targeting "src/main.py" at the new head sequence.
    // Since P1 is promoted (status = 'PROMOTED') and P2 is still on sequence 0, the active 
    // proposals at the new sequence (sequence 1) is 0. So P4 must succeed!
    let p4_res = client
        .post(format!("http://localhost:{}/v1/repos/{}/proposals", port, repo_id))
        .json(&serde_json::json!({
            "agent_id": "agent_4",
            "task_id": "task_4",
            "proposal_title": "P4",
            "proposal_description": "Testing P4",
            "base_head_id": head2_id,
            "base_head_sequence": head2_seq,
            "affected_paths": ["src/main.py"],
            "change_type": "full_file",
            "change_content": "def test_p4(): pass\n",
            "confidence_score": 0.9,
            "dependencies": [],
            "requires_tests": false,
            "requires_security_scan": false,
            "source": "conc_test"
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(p4_res.status(), 201);

    // Clean up server process
    let _ = server_process.kill().await;
}
