#!/usr/bin/env python3
import os
import sys
import time
import argparse
import random
import httpx
import json
import copy

# Add project root to python path to import packages
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from packages.aether.crdt.rga import RGADocument, ElementId, ROOT_ID
from packages.aether.crdt.document_store import DocumentStore, FileDocument

# Snapshot cache to prevent heavy concurrent disk reads
_store_cache = {}

def load_store_from_snapshot(snapshot_path, repo_id):
    crdt_state_file = os.path.join(snapshot_path, ".aether_crdt_state.json")
    store = DocumentStore(repo_id=repo_id)
    if os.path.exists(crdt_state_file):
        try:
            with open(crdt_state_file, "r") as f:
                state_dict = json.load(f)
            for path, doc_dict in state_dict.items():
                store.documents[path] = FileDocument.from_dict(doc_dict)
        except Exception as e:
            print(f"Error loading state from {crdt_state_file}: {e}")
    return store

def get_store_from_snapshot(soak_home, repo_id, snapshot_id):
    if not snapshot_id:
        return DocumentStore(repo_id=repo_id)
    
    if snapshot_id in _store_cache:
        return copy.deepcopy(_store_cache[snapshot_id])
        
    snap_path = os.path.join(soak_home, ".aether", "aether_storage", repo_id, "snapshots", snapshot_id)
    store = load_store_from_snapshot(snap_path, repo_id)
    _store_cache[snapshot_id] = store
    return copy.deepcopy(store)

def build_crdt_proposal_content(soak_home, repo_id, head_snapshot_id, file_path, agent_id, tick_seq):
    store = get_store_from_snapshot(soak_home, repo_id, head_snapshot_id)
    file_doc = store.get_document(file_path)
    doc = file_doc.document
    doc.replica_id = agent_id
    
    # Append agent tick comment
    comment = f"# agent {agent_id} tick {tick_seq}\n"
    pos = doc.get_length()
    for char in comment:
        doc.local_insert(pos, char)
        pos += 1
        
    return doc, doc.get_text()

def merge_crdt_locally(soak_home, repo_id, new_head_snapshot_id, file_path, doc_mine):
    store_theirs = get_store_from_snapshot(soak_home, repo_id, new_head_snapshot_id)
    file_doc_theirs = store_theirs.get_document(file_path)
    doc_theirs = file_doc_theirs.document
    
    # Merge my doc into theirs
    doc_theirs.merge(doc_mine)
    return doc_theirs, doc_theirs.get_text()

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-id", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--end-time", type=float, required=True)
    parser.add_argument("--interval-ms", type=int, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    parser.add_argument("--soak-home", required=True)
    parser.add_argument("--agent-dir", required=True)
    parser.add_argument("--file-path", required=True)
    return parser.parse_args()

def main():
    sys.setrecursionlimit(8000)
    args = parse_args()
    
    # Create directories
    os.makedirs(args.agent_dir, exist_ok=True)
    
    submit_ok = 0
    submit_429 = 0
    submit_fail = 0
    promote_ok = 0
    promote_409 = 0
    promote_rejected = 0
    promote_5xx = 0
    success_after_409 = 0
    conflicted_ticks = 0
    
    start_time = time.time()
    end_time = args.end_time
    seq = 0
    
    # Startup stagger to stagger load and prevent connection pool timeouts
    time.sleep(random.uniform(0.0, 8.0))
    
    with httpx.Client() as client:
        while time.time() < end_time:
            seq += 1
            
            # Paced loop with 20% jitter
            jitter = random.uniform(0.8, 1.2)
            sleep_time = (args.interval_ms / 1000.0) * jitter
            time.sleep(sleep_time)
            
            # Get head to start our proposal
            try:
                head_resp = client.get(f"{args.base_url}/v1/repos/{args.repo_id}/head", timeout=45.0)
                if head_resp.status_code != 200:
                    print(f"[{args.agent_id}] Failed to get head: {head_resp.status_code}")
                    submit_fail += 1
                    continue
                head_data = head_resp.json()
                head_id = head_data.get("head_id")
                head_seq = head_data.get("sequence", 0)
                snapshot_id = head_data.get("snapshot_id")
            except Exception as e:
                print(f"[{args.agent_id}] Error getting head: {e}")
                submit_fail += 1
                continue
                
            # Build proposal state
            doc_mine, change_content = build_crdt_proposal_content(
                args.soak_home, args.repo_id, snapshot_id, args.file_path, args.agent_id, seq
            )
            
            proposal_payload = {
                "agent_id": args.agent_id,
                "task_id": f"soak-{args.agent_id}-{seq}",
                "source": "agent_client_e2",
                "intent_type": "soak_update",
                "change_type": "full_file",
                "change_content": change_content,
                "affected_paths": [args.file_path],
                "confidence_score": 0.85,
                "rationale": f"sprint e2 soak tick {seq}",
                "base_head_id": head_id,
                "base_head_sequence": head_seq
            }
            
            # Try proposal submission with 429 backoff-rebuild-resubmit and 409 merge-repropose loops
            proposal_id = None
            attempt = 0
            had_409_conflict = False
            
            while time.time() < end_time:
                if time.time() >= end_time:
                    break
                attempt += 1
                try:
                    # 1. Submit Proposal
                    sub_resp = client.post(
                        f"{args.base_url}/v1/repos/{args.repo_id}/proposals", 
                        json=proposal_payload, 
                        timeout=45.0
                    )
                    
                    # Check for 429 throttling
                    if sub_resp.status_code == 429:
                        submit_429 += 1
                        # Extract Retry-After or backoff
                        retry_after = sub_resp.headers.get("Retry-After")
                        sleep_sec = float(retry_after) if retry_after else min(8.0, 0.5 * (2 ** attempt) + random.uniform(0, 0.5))
                        print(f"[{args.agent_id}] Submit throttled (429). Backing off for {sleep_sec:.2f}s...")
                        if time.time() + sleep_sec >= end_time:
                            break
                        time.sleep(sleep_sec)
                        
                        # Re-pull latest head and rebuild
                        h_resp = client.get(f"{args.base_url}/v1/repos/{args.repo_id}/head", timeout=45.0)
                        if h_resp.status_code == 200:
                            h_data = h_resp.json()
                            head_id = h_data.get("head_id")
                            head_seq = h_data.get("sequence", 0)
                            snapshot_id = h_data.get("snapshot_id")
                            doc_mine, change_content = build_crdt_proposal_content(
                                args.soak_home, args.repo_id, snapshot_id, args.file_path, args.agent_id, seq
                            )
                            proposal_payload["change_content"] = change_content
                            proposal_payload["base_head_id"] = head_id
                            proposal_payload["base_head_sequence"] = head_seq
                        continue
                        
                    if sub_resp.status_code not in (200, 201):
                        submit_fail += 1
                        break
                        
                    proposal_data = sub_resp.json()
                    proposal_id = proposal_data.get("proposal_id")
                    submit_ok += 1
                    
                    # 2. Request Promotion
                    if time.time() >= end_time:
                        break
                    prom_resp = client.post(
                        f"{args.base_url}/v1/repos/{args.repo_id}/promotions",
                        json={"proposal_ids": [proposal_id]},
                        timeout=45.0
                    )
                    
                    # Check for 429 throttling on promotions
                    if prom_resp.status_code == 429:
                        submit_429 += 1
                        retry_after = prom_resp.headers.get("Retry-After")
                        sleep_sec = float(retry_after) if retry_after else min(8.0, 0.5 * (2 ** attempt) + random.uniform(0, 0.5))
                        print(f"[{args.agent_id}] Promotion throttled (429). Backing off for {sleep_sec:.2f}s...")
                        if time.time() + sleep_sec >= end_time:
                            break
                        time.sleep(sleep_sec)
                        
                        # Throttling requires a complete re-pull, rebuild, and re-propose
                        h_resp = client.get(f"{args.base_url}/v1/repos/{args.repo_id}/head", timeout=45.0)
                        if h_resp.status_code == 200:
                            h_data = h_resp.json()
                            head_id = h_data.get("head_id")
                            head_seq = h_data.get("sequence", 0)
                            snapshot_id = h_data.get("snapshot_id")
                            doc_mine, change_content = build_crdt_proposal_content(
                                args.soak_home, args.repo_id, snapshot_id, args.file_path, args.agent_id, seq
                            )
                            proposal_payload["change_content"] = change_content
                            proposal_payload["base_head_id"] = head_id
                            proposal_payload["base_head_sequence"] = head_seq
                        continue
                    
                    # Check for 409 conflict
                    if prom_resp.status_code == 409:
                        promote_409 += 1
                        if not had_409_conflict:
                            had_409_conflict = True
                            conflicted_ticks += 1
                        sleep_sec = min(8.0, 0.5 * (2 ** attempt) + random.uniform(0, 0.5))
                        print(f"[{args.agent_id}] Promotion conflict (409). Backing off for {sleep_sec:.2f}s then merging...")
                        if time.time() + sleep_sec >= end_time:
                            break
                        time.sleep(sleep_sec)
                        
                        # Re-pull latest head to merge
                        h_resp = client.get(f"{args.base_url}/v1/repos/{args.repo_id}/head", timeout=45.0)
                        if h_resp.status_code == 200:
                            h_data = h_resp.json()
                            head_id = h_data.get("head_id")
                            head_seq = h_data.get("sequence", 0)
                            snapshot_id = h_data.get("snapshot_id")
                            
                            # Use production merge helper to merge our doc_mine with latest head
                            doc_mine, change_content = merge_crdt_locally(
                                args.soak_home, args.repo_id, snapshot_id, args.file_path, doc_mine
                            )
                            proposal_payload["change_content"] = change_content
                            proposal_payload["base_head_id"] = head_id
                            proposal_payload["base_head_sequence"] = head_seq
                        continue
                        
                    if prom_resp.status_code == 200:
                        prom_data = prom_resp.json()
                        if prom_data.get("success") is True:
                            promote_ok += 1
                            if had_409_conflict:
                                success_after_409 += 1
                            break
                        else:
                            promote_rejected += 1
                            break
                    elif prom_resp.status_code >= 500:
                        promote_5xx += 1
                        break
                    else:
                        promote_rejected += 1
                        break
                        
                except Exception as e:
                    print(f"[{args.agent_id}] Exception in submission/promotion loop: {e}")
                    submit_fail += 1
                    break
                
    # Write metrics to agent directory
    with open(os.path.join(args.agent_dir, "submit_ok"), "w") as f: f.write(str(submit_ok))
    with open(os.path.join(args.agent_dir, "submit_429"), "w") as f: f.write(str(submit_429))
    with open(os.path.join(args.agent_dir, "submit_fail"), "w") as f: f.write(str(submit_fail))
    with open(os.path.join(args.agent_dir, "promote_ok"), "w") as f: f.write(str(promote_ok))
    with open(os.path.join(args.agent_dir, "promote_409"), "w") as f: f.write(str(promote_409))
    with open(os.path.join(args.agent_dir, "promote_rejected"), "w") as f: f.write(str(promote_rejected))
    with open(os.path.join(args.agent_dir, "promote_5xx"), "w") as f: f.write(str(promote_5xx))
    with open(os.path.join(args.agent_dir, "success_after_409"), "w") as f: f.write(str(success_after_409))
    with open(os.path.join(args.agent_dir, "conflicted_ticks"), "w") as f: f.write(str(conflicted_ticks))
    
    print(f"[{args.agent_id}] Completed. submit_ok={submit_ok}, submit_429={submit_429}, promote_ok={promote_ok}, promote_409={promote_409}, success_after_409={success_after_409}, conflicted_ticks={conflicted_ticks}")

if __name__ == "__main__":
    main()
