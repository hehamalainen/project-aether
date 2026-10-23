#!/usr/bin/env python3
"""Run P3 fallback-readiness gate."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time
from typing import Any
from urllib import error, parse, request


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _request_json(url: str, timeout_seconds: int = 15) -> tuple[int, Any]:
    req = request.Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with request.urlopen(req, timeout=timeout_seconds) as resp:
            raw = resp.read().decode("utf-8")
            body = json.loads(raw) if raw else {}
            return resp.status, body
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {"raw": raw}
        return exc.code, body


def _request_text(url: str, timeout_seconds: int = 15) -> tuple[int, str]:
    req = request.Request(url, method="GET")
    try:
        with request.urlopen(req, timeout=timeout_seconds) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def _run(cmd: list[str], *, cwd: Path) -> tuple[bool, str]:
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=False)
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, output.strip()


def _latest_passing_artifact(directory: Path, prefix: str) -> tuple[Path | None, dict[str, Any] | None]:
    candidates = sorted(directory.glob(f"{prefix}-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if bool(payload.get("passed")):
            return path, payload
    return None, None


def run_gate(
    *,
    root: Path,
    base_url: str,
    ui_url: str,
    output_json: Path,
    pin_json: Path,
    dashboard_json: Path,
    dashboard_html: Path,
) -> dict[str, Any]:
    started = time.perf_counter()
    demo_dir = root / ".pytest-artifacts" / "demo"
    demo_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "run_id": f"p3-fallback-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}",
        "started_at": _iso_now(),
        "checks": {},
        "details": {},
        "passed": False,
    }

    ok, out = _run(["./scripts/demo_stack.sh", "status"], cwd=root)
    report["checks"]["stack_healthy"] = ok
    report["details"]["stack_status"] = out

    p1_path, p1_payload = _latest_passing_artifact(demo_dir, "p1-flow")
    report["checks"]["p1_pass_artifact_available"] = p1_path is not None and p1_payload is not None
    report["details"]["p1_pass_artifact"] = str(p1_path) if p1_path else ""

    if p1_payload is not None and p1_path is not None:
        pinned = {
            "generated_at": _iso_now(),
            "source_artifact": str(p1_path),
            "run_id": p1_payload.get("run_id"),
            "repo_id": p1_payload.get("repo_id"),
            "proposal_id": p1_payload.get("proposal_id"),
            "promotion_id": p1_payload.get("promotion_id"),
            "checks": p1_payload.get("checks", {}),
            "duration_seconds": p1_payload.get("duration_seconds"),
            "passed": bool(p1_payload.get("passed")),
        }
        pin_json.parent.mkdir(parents=True, exist_ok=True)
        pin_json.write_text(json.dumps(pinned, indent=2), encoding="utf-8")
        report["checks"]["known_good_json_pinned"] = True
        report["details"]["known_good_json"] = str(pin_json)
    else:
        report["checks"]["known_good_json_pinned"] = False
        report["details"]["known_good_json"] = ""

    health_status, health_body = _request_json(f"{base_url}/health")
    repos_status, repos_body = _request_json(f"{base_url}/repos")
    physics_status, physics_body = _request_json(
        f"{base_url}/system/physics?{parse.urlencode({'window_seconds': 60})}"
    )
    ui_status, ui_body = _request_text(ui_url)

    report["checks"]["backend_health_ok"] = health_status == 200 and isinstance(health_body, dict)
    report["checks"]["physics_visible"] = physics_status == 200 and isinstance(physics_body, dict)
    report["checks"]["ui_reachable"] = ui_status == 200

    selected_repo_id = None
    if isinstance(repos_body, list) and repos_body:
        p1_repo_id = p1_payload.get("repo_id") if isinstance(p1_payload, dict) else None
        repo_ids = [r.get("id") for r in repos_body if isinstance(r, dict)]
        if p1_repo_id in repo_ids:
            selected_repo_id = p1_repo_id
        else:
            selected_repo_id = repo_ids[0] if repo_ids else None

    head_status = 0
    head_body: Any = {}
    promotions_status = 0
    promotions_body: Any = {}
    if selected_repo_id:
        head_status, head_body = _request_json(f"{base_url}/repos/{selected_repo_id}/head")
        promotions_status, promotions_body = _request_json(f"{base_url}/repos/{selected_repo_id}/promotions")

    dashboard_snapshot = {
        "captured_at": _iso_now(),
        "base_url": base_url,
        "ui_url": ui_url,
        "health": {"status_code": health_status, "body": health_body},
        "repos": {"status_code": repos_status, "count": len(repos_body) if isinstance(repos_body, list) else 0},
        "selected_repo_id": selected_repo_id,
        "head": {"status_code": head_status, "body": head_body},
        "promotions": {"status_code": promotions_status, "body": promotions_body},
        "physics": {"status_code": physics_status, "body": physics_body},
    }
    dashboard_json.parent.mkdir(parents=True, exist_ok=True)
    dashboard_json.write_text(json.dumps(dashboard_snapshot, indent=2), encoding="utf-8")
    dashboard_html.parent.mkdir(parents=True, exist_ok=True)
    dashboard_html.write_text(ui_body, encoding="utf-8")

    stable_dashboard_json = dashboard_json.parent / "p3-dashboard-state.json"
    stable_dashboard_json.write_text(dashboard_json.read_text(encoding="utf-8"), encoding="utf-8")

    report["checks"]["dashboard_state_captured"] = True
    report["details"]["dashboard_state_json"] = str(dashboard_json)
    report["details"]["dashboard_state_json_stable"] = str(stable_dashboard_json)
    report["details"]["dashboard_state_html"] = str(dashboard_html)

    report["duration_seconds"] = round(time.perf_counter() - started, 3)
    report["ended_at"] = _iso_now()
    report["passed"] = all(bool(v) for v in report["checks"].values())

    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run P3 fallback-readiness gate.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8100/v1")
    parser.add_argument("--ui-url", default="http://127.0.0.1:3000")
    parser.add_argument("--output-json", default="")
    parser.add_argument("--pin-json", default="")
    parser.add_argument("--dashboard-json", default="")
    parser.add_argument("--dashboard-html", default="")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    output_json = Path(args.output_json) if args.output_json else root / ".pytest-artifacts" / "demo" / f"p3-fallback-{ts}.json"
    pin_json = Path(args.pin_json) if args.pin_json else root / ".pytest-artifacts" / "demo" / "p3-known-good.json"
    dashboard_json = Path(args.dashboard_json) if args.dashboard_json else root / ".pytest-artifacts" / "demo" / f"p3-dashboard-state-{ts}.json"
    dashboard_html = Path(args.dashboard_html) if args.dashboard_html else root / ".pytest-artifacts" / "demo" / f"p3-dashboard-state-{ts}.html"

    report = run_gate(
        root=root,
        base_url=args.base_url.rstrip("/"),
        ui_url=args.ui_url.rstrip("/"),
        output_json=output_json,
        pin_json=pin_json,
        dashboard_json=dashboard_json,
        dashboard_html=dashboard_html,
    )

    print(f"P3 fallback report: {output_json}")
    print(f"passed={report['passed']}")
    print("checks:")
    for key, value in report["checks"].items():
        print(f"  {key}={value}")
    print(f"known_good_json={report['details'].get('known_good_json', '')}")
    print(f"dashboard_state_json={report['details'].get('dashboard_state_json', '')}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
