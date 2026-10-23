use anyhow::Result;
use clap::{Parser, Subcommand};
use reqwest::Client;
use serde_json::json;

#[derive(Parser, Debug)]
#[command(name = "aether")]
#[command(about = "Aether Swarm CLI - Agent-native Version Control", long_about = None)]
struct Cli {
    #[command(subcommand)]
    command: Commands,

    /// Override the Aether server URL (default: http://localhost:8100)
    #[arg(short, long, env = "AETHER_SERVER_URL")]
    server_url: Option<String>,
}

#[derive(Subcommand, Debug)]
enum Commands {
    /// Manage repositories
    Repos {
        #[arg(short, long)]
        create: Option<String>,
    },
    /// Submit a proposal (used by agents to commit code)
    Push {
        #[arg(short, long)]
        repo: String,
        #[arg(short, long)]
        agent_id: String,
        #[arg(short, long)]
        confidence: f64,
        #[arg(short, long)]
        intent: String,
        /// Path to the unified diff or modified file
        #[arg(short, long)]
        file: String,
        /// Task ID for tracking
        #[arg(short, long)]
        task: Option<String>,
        /// Change type: unified_diff (default), full_file, aip.v0
        #[arg(long, default_value = "unified_diff")]
        change_type: String,
        /// Comma-separated affected paths (auto-detected from diff if omitted)
        #[arg(long)]
        paths: Option<String>,
    },
    /// Get the status of a repository or specific proposal
    Status {
        #[arg(short, long)]
        repo: String,
    },
    /// Get the current canonical head (used by agents to sync state)
    Head {
        #[arg(short, long)]
        repo: String,
    },
    /// Watch realtime events on the event bus
    Watch {
        #[arg(short, long)]
        repo: String,
    },
    /// Create or download deterministic snapshots
    Snapshot {
        #[arg(short, long)]
        repo: String,
        #[arg(short, long)]
        create: bool,
    },
    /// Explicitly request a promotion evaluation
    Promote {
        #[arg(short, long)]
        repo: String,
        #[arg(short, long)]
        proposal_id: String,
    },
    /// Run continuous high-agent flow simulation + SLO checks
    SwarmHarness {
        #[arg(short, long, default_value = "10")]
        agents: usize,
    },
}

/// Parse affected file paths from a unified diff string.
fn parse_affected_paths_from_diff(diff_content: &str) -> Vec<String> {
    let mut paths = Vec::new();
    for line in diff_content.lines() {
        if let Some(rest) = line.strip_prefix("diff --git a/") {
            // "diff --git a/path b/path" — extract the b/ side
            if let Some(b_part) = rest.split(" b/").nth(1) {
                if !b_part.is_empty() && !paths.contains(&b_part.to_string()) {
                    paths.push(b_part.to_string());
                }
            }
        }
    }
    paths
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    let base_url = cli
        .server_url
        .unwrap_or_else(|| "http://localhost:8100/v1".to_string());

    let client = Client::new();

    match &cli.command {
        Commands::Repos { create } => {
            if let Some(name) = create {
                println!("Creating repository: {}", name);
                let res = client
                    .post(format!("{}/repos", base_url))
                    .json(&json!({ "name": name }))
                    .send()
                    .await?;
                println!("{}", res.text().await?);
            } else {
                println!("Fetching repositories...");
                let res = client.get(format!("{}/repos", base_url)).send().await?;
                println!("{}", res.text().await?);
            }
        }
        Commands::Push {
            repo,
            agent_id,
            confidence,
            intent,
            file,
            task,
            change_type,
            paths,
        } => {
            println!(
                "Agent {} pushing to {} (Confidence: {})",
                agent_id, repo, confidence
            );
            println!("Intent: {}", intent);
            println!("Reading diff from: {}", file);

            let file_content = std::fs::read_to_string(file).unwrap_or_else(|e| {
                eprintln!("Failed to read file '{}': {}", file, e);
                std::process::exit(1);
            });

            // Resolve affected paths: explicit flag > parsed from diff > empty
            let affected_paths: Vec<String> = if let Some(p) = paths {
                p.split(',')
                    .map(|s| s.trim().to_string())
                    .filter(|s| !s.is_empty())
                    .collect()
            } else if change_type == "unified_diff" {
                parse_affected_paths_from_diff(&file_content)
            } else {
                vec![]
            };

            let mut payload = json!({
                "agent_id": agent_id,
                "confidence_score": confidence,
                "intent_type": intent,
                "change_type": change_type,
                "change_content": file_content,
                "affected_paths": affected_paths,
                "source": "manual"
            });

            if let Some(t) = task {
                payload["task_id"] = json!(t);
            }

            let res = client
                .post(format!("{}/repos/{}/proposals", base_url, repo))
                .json(&payload)
                .send()
                .await?;
            println!("{}", res.text().await?);
        }
        Commands::Status { repo } => {
            println!("Checking status for repo: {}", repo);
        }
        Commands::Head { repo } => {
            println!("Fetching head for repo: {}", repo);
            let res = client
                .get(format!("{}/repos/{}/head", base_url, repo))
                .send()
                .await?;
            println!("{}", res.text().await?);
        }
        Commands::Watch { repo } => {
            println!("Watching events for repo: {}", repo);
        }
        Commands::Snapshot { repo, create } => {
            if *create {
                println!("Creating snapshot for repo: {}", repo);
            } else {
                println!("Listing snapshots for repo: {}", repo);
            }
        }
        Commands::Promote { repo, proposal_id } => {
            println!(
                "Requesting promotion for proposal {} in repo {}",
                proposal_id, repo
            );
        }
        Commands::SwarmHarness { agents } => {
            println!("Starting Swarm Harness with {} concurrent agents", agents);
            println!("(This will simulate massive parallel pushes to stress-test the CRDT engine)");
        }
    }

    Ok(())
}
