#!/usr/bin/env python3
"""Run P1 demo rehearsal gate: repo -> proposal -> promotion -> physics."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any
from urllib import error, parse, request


@dataclass
class ApiResponse:
    status_code: int
    body: Any


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _request_json(
    *,
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    timeout_seconds: int = 20,
) -> ApiResponse:
    data: bytes | None = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = request.Request(url, data=data, method=method, headers=headers)
    try:
        with request.urlopen(req, timeout=timeout_seconds) as resp:
            raw = resp.read().decode("utf-8")
            body = json.loads(raw) if raw else {}
            return ApiResponse(status_code=resp.status, body=body)
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {"raw": raw}
        return ApiResponse(status_code=exc.code, body=body)
    except error.URLError as exc:
        raise RuntimeError(f"request failed for {url}: {exc}") from exc


def _record_step(
    report: dict[str, Any],
    *,
    step: str,
    status_code: int,
    response: Any,
    started: float,
) -> None:
    report["steps"][step] = {
        "status_code": status_code,
        "duration_ms": round((time.perf_counter() - started) * 1000.0, 2),
        "response": response,
    }


def run_flow(*, base_url: str, output_json: Path, timeout_seconds: int) -> dict[str, Any]:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_started = time.perf_counter()

    report: dict[str, Any] = {
        "run_id": f"p1-demo-flow-{timestamp}",
        "started_at": _iso_now(),
        "base_url": base_url,
        "checks": {},
        "steps": {},
        "passed": False,
    }

    repo_payload = {
        "name": f"p1-demo-{timestamp}",
        "description": "P1 demo rehearsal flow",
        "config": {
            "gates": [
                {
                    "name": "syntax",
                    "command": "true",
                    "required": True,
                    "timeout_seconds": 1,
                    "order": 0,
                }
            ],
            "auto_promote": True,
            "promotion_threshold": 0.6,
        },
    }
    started = time.perf_counter()
    repo_resp = _request_json(
        method="POST",
        url=f"{base_url}/repos",
        payload=repo_payload,
        timeout_seconds=timeout_seconds,
    )
    _record_step(report, step="create_repo", status_code=repo_resp.status_code, response=repo_resp.body, started=started)
    repo_id = repo_resp.body.get("id") if isinstance(repo_resp.body, dict) else None
    report["checks"]["repo_created"] = repo_resp.status_code == 200 and isinstance(repo_id, str) and repo_id.startswith("repo_")

    proposal_resp = ApiResponse(status_code=0, body={})
    proposal_id: str | None = None
    if report["checks"]["repo_created"]:
        proposal_payload = {
            "agent_id": "demo-agent-01",
            "task_id": "p1-rehearsal",
            "change_type": "full_file",
            "change_content": (
                '"""P1 demo file."""\n\n'
                "def demo_signal() -> str:\n"
                f'    return "demo-ok-{timestamp}"\n'
            ),
            "affected_paths": ["src/demo/p1_flow.py"],
            "confidence_score": 0.95,
            "requires_tests": False,
        }
        started = time.perf_counter()
        proposal_resp = _request_json(
            method="POST",
            url=f"{base_url}/repos/{repo_id}/proposals",
            payload=proposal_payload,
            timeout_seconds=timeout_seconds,
        )
        _record_step(
            report,
            step="submit_proposal",
            status_code=proposal_resp.status_code,
            response=proposal_resp.body,
            started=started,
        )
        proposal_id = proposal_resp.body.get("proposal_id") if isinstance(proposal_resp.body, dict) else None
    report["checks"]["proposal_submitted"] = (
        proposal_resp.status_code == 200
        and isinstance(proposal_id, str)
        and proposal_id.startswith("prop_")
    )

    promotion_resp = ApiResponse(status_code=0, body={})
    promotion_id: str | None = None
    if report["checks"]["proposal_submitted"]:
        started = time.perf_counter()
        promotion_resp = _request_json(
            method="POST",
            url=f"{base_url}/repos/{repo_id}/promotions",
            payload={"proposal_ids": [proposal_id], "force": True},
            timeout_seconds=max(timeout_seconds, 30),
        )
        _record_step(
            report,
            step="request_promotion",
            status_code=promotion_resp.status_code,
            response=promotion_resp.body,
            started=started,
        )
        if isinstance(promotion_resp.body, dict):
            promotion_id = promotion_resp.body.get("promotion_id")
    report["checks"]["promotion_success"] = (
        promotion_resp.status_code == 200
        and isinstance(promotion_resp.body, dict)
        and bool(promotion_resp.body.get("success"))
        and isinstance(promotion_id, str)
        and promotion_id.startswith("prom_")
    )

    head_resp = ApiResponse(status_code=0, body={})
    if report["checks"]["repo_created"]:
        started = time.perf_counter()
        head_resp = _request_json(
            method="GET",
            url=f"{base_url}/repos/{repo_id}/head",
            timeout_seconds=timeout_seconds,
        )
        _record_step(report, step="get_head", status_code=head_resp.status_code, response=head_resp.body, started=started)
    head_seq = head_resp.body.get("sequence", 0) if isinstance(head_resp.body, dict) else 0
    report["checks"]["head_advanced"] = head_resp.status_code == 200 and isinstance(head_seq, int) and head_seq >= 1

    started = time.perf_counter()
    physics_url = f"{base_url}/system/physics?{parse.urlencode({'window_seconds': 60})}"
    physics_resp = _request_json(
        method="GET",
        url=physics_url,
        timeout_seconds=timeout_seconds,
    )
    _record_step(report, step="get_physics", status_code=physics_resp.status_code, response=physics_resp.body, started=started)
    required_physics_keys = {"lambda_tps", "mu_tps", "queue_depth", "events", "fallback_rate"}
    report["checks"]["physics_visible"] = (
        physics_resp.status_code == 200
        and isinstance(physics_resp.body, dict)
        and required_physics_keys.issubset(set(physics_resp.body.keys()))
    )

    report["duration_seconds"] = round(time.perf_counter() - run_started, 3)
    report["ended_at"] = _iso_now()
    report["repo_id"] = repo_id
    report["proposal_id"] = proposal_id
    report["promotion_id"] = promotion_id
    report["passed"] = all(bool(v) for v in report["checks"].values())

    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run P1 demo rehearsal gate.")
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8100/v1",
        help="Aether API base URL (default: http://127.0.0.1:8100/v1)",
    )
    parser.add_argument(
        "--output-json",
        default="",
        help="Optional output report path. Defaults to .pytest-artifacts/demo/p1-flow-<timestamp>.json",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=20,
        help="Per-request timeout in seconds (default: 20)",
    )
    args = parser.parse_args()

    if args.output_json:
        output = Path(args.output_json)
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        output = Path(".pytest-artifacts/demo") / f"p1-flow-{ts}.json"

    report = run_flow(
        base_url=args.base_url.rstrip("/"),
        output_json=output,
        timeout_seconds=max(5, int(args.timeout_seconds)),
    )

    print(f"P1 flow report: {output}")
    print(f"passed={report['passed']}")
    print(f"repo_id={report.get('repo_id')}")
    print(f"proposal_id={report.get('proposal_id')}")
    print(f"promotion_id={report.get('promotion_id')}")
    print(f"duration_seconds={report.get('duration_seconds')}")
    print("checks:")
    for key, value in report["checks"].items():
        print(f"  {key}={value}")

    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
