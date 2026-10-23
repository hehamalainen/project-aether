use crdt::causality::{Dot, VersionVector};
use crdt::head_crdt::{HeadEvent, HeadFrontier};
use rand::{Rng, SeedableRng};
use rand::rngs::StdRng;

#[test]
fn test_stochastic_compaction_efficiency() {
    let mut rng = StdRng::seed_from_u64(42);
    
    let num_agents = 50;
    let num_steps = 1000;
    let merge_probability = 0.2; // 20% chance to fetch the shared frontier before promoting
    
    let mut shared_frontier = HeadFrontier::new();
    let mut agent_sequences = vec![0; num_agents];
    let mut agent_contexts = vec![VersionVector::new(); num_agents];
    
    let mut max_frontier_width = 0;
    
    for _ in 0..num_steps {
        let agent_idx = rng.gen_range(0..num_agents);
        let agent_id = format!("agent_{}", agent_idx);
        
        // 1. Possibly observe the shared state
        if rng.gen_bool(merge_probability) {
            let shared_ctx = shared_frontier.causal_context();
            agent_contexts[agent_idx].merge(&shared_ctx);
        }
        
        // 2. Promote
        agent_sequences[agent_idx] += 1;
        let seq = agent_sequences[agent_idx];
        
        // The event's causal context is what the agent currently knows
        let event = HeadEvent {
            snapshot_id: format!("snap_{}_{}", agent_id, seq),
            dot: Dot::new(agent_id.clone(), seq),
            context: agent_contexts[agent_idx].clone(),
            agent_id: agent_id.clone(),
            frozen_trust: rng.gen_range(0.0..1.0),
        };
        
        // 3. Update the shared frontier
        let mut new_f = HeadFrontier::new();
        new_f.append(event);
        shared_frontier.join(&new_f);
        
        // Also update the agent's own context to reflect its own promotion
        agent_contexts[agent_idx].increment(&agent_id);
        
        if shared_frontier.events.len() > max_frontier_width {
            max_frontier_width = shared_frontier.events.len();
        }
    }
    
    // With a 20% merge probability, the frontier should NOT grow indefinitely to 1000.
    // It should compact frequently.
    assert!(max_frontier_width < num_agents * 2, "Frontier exploded. Compaction failed.");
    assert!(!shared_frontier.events.is_empty());
}

#[test]
fn test_simulation_zero_merge_frequency() {
    // If no agents ever observe the shared state, the frontier width MUST grow to N.
    // They still observe their own previous states, so each agent has exactly 1 active head.
    let num_agents = 10;
    let num_steps = 100;
    
    let mut shared_frontier = HeadFrontier::new();
    let mut agent_contexts = vec![VersionVector::new(); num_agents];
    let mut agent_sequences = vec![0; num_agents];
    
    for i in 0..num_steps {
        let agent_idx = i % num_agents;
        let agent_id = format!("agent_{}", agent_idx);
        
        agent_sequences[agent_idx] += 1;
        let seq = agent_sequences[agent_idx];
        
        let event = HeadEvent {
            snapshot_id: format!("snap_{}_{}", agent_id, seq),
            dot: Dot::new(agent_id.clone(), seq),
            context: agent_contexts[agent_idx].clone(), // Only observed own past
            agent_id: agent_id.clone(),
            frozen_trust: 1.0,
        };
        
        let mut new_f = HeadFrontier::new();
        new_f.append(event);
        shared_frontier.join(&new_f);
        
        agent_contexts[agent_idx].increment(&agent_id);
    }
    
    assert_eq!(shared_frontier.events.len(), num_agents, "Frontier should equal N since there's zero causal dominance across agents.");
}
