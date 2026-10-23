"""
End-to-End test for MX Swarm → Aether VCS Integration.

This script verifies that the VCS adapter can successfully submit
proposals to a running Aether server.
"""
import asyncio
import json
import os
import sys
from pathlib import Path
import pytest

# Add root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from packages.aether.adapters import create_vcs_adapter
from packages.aether.adapters.vcs_adapter import VCSConfig

# Default test configuration
AETHER_BASE_URL = "http://localhost:8100"
TEST_REPO_NAME = "aether-demo"

# This module is an opt-in live-server E2E harness.
pytestmark = pytest.mark.skipif(
    os.getenv("AETHER_ENABLE_E2E_TESTS") != "1",
    reason="Set AETHER_ENABLE_E2E_TESTS=1 and run against a live Aether server",
)


async def get_or_create_repo(base_url: str, repo_name: str) -> str:
    """Find existing repo by name or create it, return repo_id."""
    import httpx
    
    async with httpx.AsyncClient(timeout=10.0) as client:
        # List existing repos
        response = await client.get(f"{base_url}/repos")
        if response.status_code == 200:
            repos = response.json()
            for repo in repos:
                if repo["name"] == repo_name:
                    return repo["id"]
        
        # Create new repo
        response = await client.post(
            f"{base_url}/repos",
            json={"name": repo_name, "description": "E2E Test Repository"}
        )
        if response.status_code == 200:
            return response.json()["id"]
        
        raise RuntimeError(f"Failed to get or create repo: {response.text}")


async def test_aether_health_check():
    """Test that Aether server is reachable."""
    print("=" * 60)
    print("🧪 Test 1: Aether Health Check")
    print("=" * 60)
    
    config = VCSConfig(
        provider="aether",
        aether_base_url=AETHER_BASE_URL,
        aether_repo_id="placeholder"  # Just for health check
    )
    
    adapter = create_vcs_adapter(config)
    healthy = await adapter.health_check()
    
    if healthy:
        print("✅ Aether server is healthy and reachable")
        return True
    else:
        print("❌ Aether server is NOT reachable")
        print("   Make sure to start the server with:")
        print("   cd apps/aether_server && python server.py")
        return False


async def test_proposal_submission(repo_id: str):
    """Test submitting a proposal to Aether."""
    print("\n" + "=" * 60)
    print("🧪 Test 2: Proposal Submission")
    print("=" * 60)
    print(f"   Using repo: {repo_id}")
    
    config = VCSConfig(
        provider="aether",
        aether_base_url=AETHER_BASE_URL,
        aether_repo_id=repo_id
    )
    
    adapter = create_vcs_adapter(config)
    
    # Sample diff
    test_diff = """diff --git a/test.py b/test.py
--- a/test.py
+++ b/test.py
@@ -1,3 +1,5 @@
 def hello():
-    print("Hello")
+    print("Hello, Aether!")
+    print("Agent-native VCS is the future!")
"""
    
    result = await adapter.submit_change(
        diff=test_diff,
        confidence=0.85,
        agent_id="test-agent-001",
        task_id="e2e-test-task",
        paths=["test.py"],
        metadata={
            "model": "e2e-test",
            "dependencies": []
        }
    )
    
    print(f"   Success: {result.success}")
    print(f"   Proposal ID: {result.proposal_id}")
    print(f"   Status: {result.status.value}")
    print(f"   Message: {result.message}")
    
    if result.success:
        print("✅ Proposal submitted successfully!")
        return result.proposal_id
    else:
        print("❌ Proposal submission failed")
        return None


async def test_proposal_status(repo_id: str, proposal_id: str):
    """Test retrieving proposal status from Aether."""
    print("\n" + "=" * 60)
    print("🧪 Test 3: Proposal Status Check")
    print("=" * 60)
    
    if not proposal_id:
        print("⚠️  Skipping - no proposal ID from previous test")
        return
    
    config = VCSConfig(
        provider="aether",
        aether_base_url=AETHER_BASE_URL,
        aether_repo_id=repo_id
    )
    
    adapter = create_vcs_adapter(config)
    result = await adapter.get_status(proposal_id)
    
    print(f"   Success: {result.success}")
    print(f"   Status: {result.status.value}")
    
    if result.success:
        print("✅ Status retrieved successfully!")
    else:
        print(f"❌ Status check failed: {result.message}")


async def test_factory_github():
    """Test that GitHub adapter can be created (without actual submission)."""
    print("\n" + "=" * 60)
    print("🧪 Test 4: GitHub Adapter Factory")
    print("=" * 60)
    
    try:
        config = VCSConfig(
            provider="github",
            github_token="fake-token-for-test",
            github_repo="test/repo"
        )
        
        adapter = create_vcs_adapter(config)
        print(f"   Adapter type: {type(adapter).__name__}")
        print("✅ GitHub adapter created successfully!")
        return True
    except Exception as e:
        print(f"❌ Failed to create GitHub adapter: {e}")
        return False


def test_aether_vcs_normalization():
    """Test that base_url is normalized correctly and suffix /v1 is stripped."""
    print("=" * 60)
    print("🧪 Test: Aether VCS URL Normalization")
    print("=" * 60)
    
    # 1. Test normalization when /v1 is passed
    config_with_v1 = VCSConfig(
        provider="aether",
        aether_base_url="http://localhost:8100/v1",
        aether_repo_id="test-repo"
    )
    adapter1 = create_vcs_adapter(config_with_v1)
    assert adapter1.base_url == "http://localhost:8100"
    print("✅ Normalized legacy /v1 base URL successfully")
    
    # 2. Test when server root is passed directly
    config_without_v1 = VCSConfig(
        provider="aether",
        aether_base_url="http://localhost:8100",
        aether_repo_id="test-repo"
    )
    adapter2 = create_vcs_adapter(config_without_v1)
    assert adapter2.base_url == "http://localhost:8100"
    print("✅ Handled clean server root base URL successfully")
    return True


async def main():
    """Run all E2E tests."""
    print("\n" + "🚀" * 30)
    print("  MX Swarm → Aether VCS Integration E2E Tests")
    print("🚀" * 30 + "\n")
    
    # Run URL normalization unit test first
    test_aether_vcs_normalization()
    
    # Test 1: Health check
    healthy = await test_aether_health_check()
    
    proposal_id = None
    repo_id = None
    
    if healthy:
        # Get or create test repository
        try:
            print("\n📦 Setting up test repository...")
            repo_id = await get_or_create_repo(f"{AETHER_BASE_URL}/v1", TEST_REPO_NAME)
            print(f"   Using repo: {repo_id}")
        except Exception as e:
            print(f"❌ Failed to setup repository: {e}")
            healthy = False
    
    if healthy and repo_id:
        # Test 2: Submit proposal
        proposal_id = await test_proposal_submission(repo_id)
        
        # Test 3: Check status
        await test_proposal_status(repo_id, proposal_id)
    
    # Test 4: Factory test (doesn't need server)
    await test_factory_github()
    
    # Summary
    print("\n" + "=" * 60)
    print("📊 Test Summary")
    print("=" * 60)
    
    if healthy and proposal_id:
        print("✅ All integration tests PASSED!")
        print("\nThe MX Swarm → Aether integration is working correctly.")
        return 0
    elif not healthy:
        print("⚠️  Server not reachable - start Aether server first")
        print("\nTo start the server:")
        print("  cd apps/aether_server")
        print("  python server.py")
        return 1
    else:
        print("❌ Some tests FAILED")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
