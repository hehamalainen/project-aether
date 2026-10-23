from packages.aether.soak_policy import (
    CANONICAL_BASELINE,
    MIXED_SOURCE_STRESS,
    evaluate_soak_policy,
)


def test_short_run_is_allowed_even_with_mx_overlap() -> None:
    decision = evaluate_soak_policy(
        run_seconds=3600,
        run_class=CANONICAL_BASELINE,
        current_hour=14,
        process_snapshot="123 scale-worker-load --logical-workers 100",
    )

    assert decision.allowed is True
    assert decision.reason == "short_run"


def test_canonical_long_run_requires_night_window() -> None:
    decision = evaluate_soak_policy(
        run_seconds=28800,
        run_class=CANONICAL_BASELINE,
        current_hour=14,
        process_snapshot="",
    )

    assert decision.allowed is False
    assert decision.reason == "daytime_long_run_blocked"


def test_canonical_long_run_blocks_mx_overlap() -> None:
    decision = evaluate_soak_policy(
        run_seconds=28800,
        run_class=CANONICAL_BASELINE,
        current_hour=22,
        process_snapshot="123 ./.venv/bin/python -m cli.main scale-worker-load --logical-workers 100",
    )

    assert decision.allowed is False
    assert decision.reason == "mx_overlap_detected"
    assert decision.matched_processes


def test_mixed_source_long_run_requires_explicit_opt_in() -> None:
    decision = evaluate_soak_policy(
        run_seconds=28800,
        run_class=MIXED_SOURCE_STRESS,
        current_hour=22,
        process_snapshot="123 ./.venv/bin/python -m cli.main scale-worker-load --logical-workers 100",
    )

    assert decision.allowed is False
    assert decision.reason == "mixed_source_requires_explicit_opt_in"


def test_mixed_source_long_run_allows_overlap_with_opt_in() -> None:
    decision = evaluate_soak_policy(
        run_seconds=28800,
        run_class=MIXED_SOURCE_STRESS,
        current_hour=22,
        process_snapshot="123 ./.venv/bin/python -m cli.main scale-worker-load --logical-workers 100",
        allow_mixed_source_stress=True,
    )

    assert decision.allowed is True
    assert decision.reason == "allowed"
    assert decision.matched_processes


def test_canonical_long_run_allows_night_when_idle() -> None:
    decision = evaluate_soak_policy(
        run_seconds=28800,
        run_class=CANONICAL_BASELINE,
        current_hour=23,
        process_snapshot="123 /usr/libexec/some-safe-process",
    )

    assert decision.allowed is True
    assert decision.reason == "allowed"
