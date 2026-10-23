"""Regression tests for Aether security defaults and trusted-execution boundaries."""

from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine

from apps.aether_server.main import (
    DEFAULT_CORS_ORIGINS,
    DEFAULT_SERVER_HOST,
    _resolve_cors_origins,
    _resolve_server_host,
    app,
)
from packages.aether.api.routes import set_services
from packages.aether.api.websocket import ConnectionManager, set_connection_manager
from packages.aether.models.entities import GateConfig, GateStatus, Repository, Snapshot
from packages.aether.services.event_bus import EventBus
from packages.aether.services.gate_runner import GateRunner, is_safe_simulation_command


@pytest.fixture
def api_client(tmp_path):
    """Create an isolated TestClient wired to a temporary SQLite database."""
    db_path = tmp_path / "security_test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    event_bus = EventBus()
    storage_path = tmp_path / "storage"
    storage_path.mkdir(parents=True, exist_ok=True)
    set_services(engine, event_bus, storage_path)
    set_connection_manager(ConnectionManager(event_bus))
    return TestClient(app)


def test_default_server_host_is_loopback(monkeypatch):
    """Server must bind to 127.0.0.1 by default and reject unsafe network binds without auth."""
    monkeypatch.delenv("AETHER_HOST", raising=False)
    monkeypatch.delenv("AETHER_ALLOW_UNSAFE_NETWORK_BIND", raising=False)
    monkeypatch.delenv("AETHER_API_TOKEN", raising=False)

    assert DEFAULT_SERVER_HOST == "127.0.0.1"
    assert _resolve_server_host() == "127.0.0.1"
    assert _resolve_server_host("localhost") == "localhost"
    assert _resolve_server_host("::1") == "::1"

    with pytest.raises(RuntimeError, match="Refusing to bind Aether server to non-loopback host"):
        _resolve_server_host("0.0.0.0")

    monkeypatch.setenv("AETHER_ALLOW_UNSAFE_NETWORK_BIND", "true")
    with pytest.raises(RuntimeError, match="without AETHER_API_TOKEN set"):
        _resolve_server_host("0.0.0.0")

    monkeypatch.setenv("AETHER_API_TOKEN", "test-secret-token")
    assert _resolve_server_host("0.0.0.0") == "0.0.0.0"


def test_cors_origins_restricted_by_default(monkeypatch, api_client):
    """CORS must default to localhost origins and reject wildcards without explicit opt-in."""
    monkeypatch.delenv("AETHER_CORS_ORIGINS", raising=False)
    monkeypatch.delenv("AETHER_ALLOW_UNSAFE_CORS_WILDCARD", raising=False)

    origins = _resolve_cors_origins()
    assert origins == DEFAULT_CORS_ORIGINS
    assert "*" not in origins

    monkeypatch.setenv("AETHER_CORS_ORIGINS", "*")
    with pytest.raises(RuntimeError, match="Wildcard AETHER_CORS_ORIGINS"):
        _resolve_cors_origins()

    # Verify preflight behavior on the FastAPI app
    evil_resp = api_client.options(
        "/v1/repos",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert evil_resp.headers.get("access-control-allow-origin") is None

    local_resp = api_client.options(
        "/v1/repos",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert local_resp.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_api_rejects_custom_gate_commands_by_default(monkeypatch, api_client):
    """Unauthenticated callers cannot register arbitrary shell commands via POST /v1/repos."""
    monkeypatch.delenv("AETHER_ALLOW_CUSTOM_GATE_COMMANDS", raising=False)

    resp = api_client.post(
        "/v1/repos",
        json={
            "name": "untrusted-repo",
            "config": {
                "gates": [
                    {"name": "exploit", "command": "id > /tmp/pwned", "required": True},
                ]
            },
        },
    )
    assert resp.status_code == 403
    assert "AETHER_ALLOW_CUSTOM_GATE_COMMANDS=true" in resp.json()["detail"]

    # Shell metacharacter injection inside echo must also be rejected
    resp_inject = api_client.post(
        "/v1/repos",
        json={
            "name": "inject-repo",
            "config": {
                "gates": [
                    {"name": "inject", "command": "echo ok; uname -a", "required": True},
                ]
            },
        },
    )
    assert resp_inject.status_code == 403

    # Safe simulated gates succeed
    resp_safe = api_client.post(
        "/v1/repos",
        json={
            "name": "safe-repo",
            "config": {
                "gates": [
                    {"name": "sim", "command": "echo 'build skipped'", "required": True},
                ]
            },
        },
    )
    assert resp_safe.status_code == 200


@pytest.mark.asyncio
async def test_gate_runner_blocks_custom_commands_by_default_and_scrubs_env(monkeypatch, tmp_path):
    """GateRunner must block custom shell commands by default and scrub sensitive env vars when enabled."""
    monkeypatch.delenv("AETHER_ALLOW_CUSTOM_GATE_COMMANDS", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-sensitive-secret-value")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_sensitive_token_value")
    monkeypatch.setenv("AETHER_API_TOKEN", "internal-api-token")

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    event_bus = EventBus()

    with Session(engine) as session:
        repo = Repository(id="repo_sec", name="sec")
        snap = Snapshot(
            id="snap_sec",
            repo_id=repo.id,
            head_sequence=0,
            content_hash="abc",
            file_count=0,
            total_bytes=0,
        )
        session.add(repo)
        session.add(snap)
        session.commit()

        marker = tmp_path / "should_not_exist.txt"
        default_runner = GateRunner(session, event_bus)
        res = await default_runner.run_gate(
            snap.id,
            GateConfig(name="custom", command=f"touch {marker}", required=True),
            tmp_path,
        )
        assert res.status == GateStatus.ERROR
        assert res.exit_code == -1
        assert "disabled by default" in res.stderr
        assert not marker.exists()

        # When trusted execution is enabled, sensitive server env vars are still scrubbed
        trusted_runner = GateRunner(session, event_bus, allow_custom_commands=True)
        scrubbed_env = trusted_runner._build_gate_env(
            snapshot_id=snap.id,
            gate_name="custom",
            proposal_id="prop_1",
            env_vars=None,
        )
        assert "OPENAI_API_KEY" not in scrubbed_env
        assert "GITHUB_TOKEN" not in scrubbed_env
        assert "AETHER_API_TOKEN" not in scrubbed_env
        assert scrubbed_env["AETHER_SNAPSHOT_ID"] == snap.id


def test_api_token_auth_middleware(monkeypatch, api_client):
    """When AETHER_API_TOKEN is set, protected endpoints require a matching token."""
    monkeypatch.setenv("AETHER_API_TOKEN", "release-token-123")

    # Health check remains accessible
    assert api_client.get("/v1/health").status_code == 200

    # Protected endpoint without token -> 401
    assert api_client.get("/v1/repos").status_code == 401

    # Protected endpoint with wrong token -> 401
    assert api_client.get("/v1/repos", headers={"Authorization": "Bearer wrong"}).status_code == 401

    # Protected endpoint with valid Bearer token -> 200
    assert (
        api_client.get("/v1/repos", headers={"Authorization": "Bearer release-token-123"}).status_code
        == 200
    )


def test_websocket_authentication_and_origin_validation(monkeypatch, api_client):
    """Both WebSocket endpoints must enforce AETHER_API_TOKEN and Origin checks before accept()."""
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setenv("AETHER_API_TOKEN", "release-token-123")
    monkeypatch.delenv("AETHER_CORS_ORIGINS", raising=False)
    monkeypatch.delenv("AETHER_ALLOW_UNSAFE_CORS_WILDCARD", raising=False)

    ws_paths = ["/v1/events", "/v1/repos/repo_test/events"]

    for ws_path in ws_paths:
        # 1. Missing credentials -> rejected before accept()
        with pytest.raises(WebSocketDisconnect) as exc_missing:
            with api_client.websocket_connect(ws_path):
                pass
        assert exc_missing.value.code == 1008

        # 2. Incorrect credentials (header and query param) -> rejected before accept()
        with pytest.raises(WebSocketDisconnect) as exc_wrong_hdr:
            with api_client.websocket_connect(
                ws_path, headers={"Authorization": "Bearer wrong-token"}
            ):
                pass
        assert exc_wrong_hdr.value.code == 1008

        with pytest.raises(WebSocketDisconnect) as exc_wrong_query:
            with api_client.websocket_connect(f"{ws_path}?token=wrong-token"):
                pass
        assert exc_wrong_query.value.code == 1008

        # 3. Valid token but untrusted browser Origin -> rejected before accept()
        with pytest.raises(WebSocketDisconnect) as exc_bad_origin:
            with api_client.websocket_connect(
                ws_path,
                headers={
                    "Authorization": "Bearer release-token-123",
                    "Origin": "https://evil.example.com",
                },
            ):
                pass
        assert exc_bad_origin.value.code == 1008

        # 4. Valid Bearer token and allowed Origin -> accepted and serves get_recent
        with api_client.websocket_connect(
            ws_path,
            headers={
                "Authorization": "Bearer release-token-123",
                "Origin": "http://localhost:3000",
            },
        ) as ws:
            welcome = ws.receive_json()
            assert welcome["type"] == "connected"
            ws.send_json({"type": "get_recent", "limit": 5})
            recent = ws.receive_json()
            assert recent["type"] == "recent_events"

        # 5. Valid X-Aether-Token header -> accepted
        with api_client.websocket_connect(
            ws_path,
            headers={"X-Aether-Token": "release-token-123"},
        ) as ws:
            welcome = ws.receive_json()
            assert welcome["type"] == "connected"

        # 6. Valid ?token= query parameter -> accepted
        with api_client.websocket_connect(f"{ws_path}?token=release-token-123") as ws:
            welcome = ws.receive_json()
            assert welcome["type"] == "connected"



def test_is_safe_simulation_command():
    """Verify simulation command allowlist rejects shell metacharacters."""
    assert is_safe_simulation_command("echo 'build skipped'") is True
    assert is_safe_simulation_command("true") is True
    assert is_safe_simulation_command("exit 0") is True
    assert is_safe_simulation_command("SIMULATED: syntax check") is True
    assert is_safe_simulation_command("echo ok; id") is False
    assert is_safe_simulation_command("echo $(whoami)") is False
    assert is_safe_simulation_command("python3 -m pytest") is False


def test_rust_server_enforces_equivalent_network_bind_and_token_protections():
    """Both Rust server entrypoints (main.rs and db_postgres.rs) must enforce bind host and API token checks."""
    repo_root = Path(__file__).resolve().parents[2]
    main_rs = (repo_root / "aether-rs/crates/server/src/main.rs").read_text(encoding="utf-8")
    pg_rs = (repo_root / "aether-rs/crates/server/src/db_postgres.rs").read_text(encoding="utf-8")

    assert "resolve_server_host_from_env()?" in main_rs
    assert "resolve_server_host_from_env()?" in pg_rs
    assert "api_token_auth_middleware" in main_rs
    assert "api_token_auth_middleware" in pg_rs
    assert "AETHER_ALLOW_UNSAFE_NETWORK_BIND" in main_rs
    assert "AETHER_API_TOKEN" in main_rs

