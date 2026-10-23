from apps.aether_server.main import _resolve_worker_configs


def test_default_server_profile_keeps_autopromote_disabled(monkeypatch):
    monkeypatch.delenv("AETHER_SERVER_PROFILE", raising=False)
    monkeypatch.delenv("AETHER_AUTOPROMOTE_ENABLED", raising=False)
    monkeypatch.delenv("AETHER_AUTOPROMOTE_INTERVAL_SECONDS", raising=False)

    profile, materialize_config, autopromote_config = _resolve_worker_configs()

    assert profile == "default"
    assert materialize_config.enabled is True
    assert autopromote_config.enabled is False
    assert autopromote_config.interval_seconds == 15


def test_mx_proof_profile_enables_fast_autopromote_defaults(monkeypatch):
    monkeypatch.setenv("AETHER_SERVER_PROFILE", "mx_proof")
    monkeypatch.delenv("AETHER_AUTOPROMOTE_ENABLED", raising=False)
    monkeypatch.delenv("AETHER_AUTOPROMOTE_INTERVAL_SECONDS", raising=False)

    profile, _, autopromote_config = _resolve_worker_configs()

    assert profile == "mx_proof"
    assert autopromote_config.enabled is True
    assert autopromote_config.interval_seconds == 5


def test_mx_proof_profile_respects_explicit_autopromote_overrides(monkeypatch):
    monkeypatch.setenv("AETHER_SERVER_PROFILE", "mx_proof")
    monkeypatch.setenv("AETHER_AUTOPROMOTE_ENABLED", "false")
    monkeypatch.setenv("AETHER_AUTOPROMOTE_INTERVAL_SECONDS", "9")

    profile, _, autopromote_config = _resolve_worker_configs()

    assert profile == "mx_proof"
    assert autopromote_config.enabled is False
    assert autopromote_config.interval_seconds == 9
