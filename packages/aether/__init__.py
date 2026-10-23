"""
Project Aether - Probabilistic, CRDT-Powered Version Control for Agent Swarms

Aether is an agent-native version control system that replaces "branches + merges" with:
1. CRDT-based concurrency for conflict-free replication of edits
2. Probabilistic proposals: agents submit changes with uncertainty signals
3. Selection & promotion: evaluates competing proposals using hard gates and promotes best-supported state
"""

__version__ = "0.1.0"
