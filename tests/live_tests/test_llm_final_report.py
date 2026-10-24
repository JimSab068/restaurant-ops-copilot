"""
LLM-based post-deployment acceptance test.

Exercises the deployed HTTP API with deterministic and real Bedrock cycles.
The report records the target URL and requires evidence that Bedrock was
actually called without errors.

Prerequisites (this script does NOT do these for you):
    1. Set OPS_COPILOT_BASE_URL to the deployed ALB URL.
    2. Retrieve the latest tenant fixture secret as tenant_fixtures.json,
       or provide credentials in either of these formats:

           {
             "<tenant_id>": {"api_key": "<raw_api_key>"}
           }

       The simpler {"<tenant_id>": "<raw_api_key>"} format also works.
       Keep fixture files private; they contain API keys.
    3. The deployed API task role must be able to invoke the configured
       Bedrock model. The laptop running this script needs no AWS credentials.

Usage:
    export OPS_COPILOT_BASE_URL=http://<alb-dns-name>
    export OPS_COPILOT_CREDENTIALS_FILE=./tenant_fixtures.json
    python tests/live_tests/test_llm_final_report.py
    python tests/live_tests/test_llm_final_report.py --tenant-id <eligible-tenant-id>

Writes:
    llm_acceptance_report_<timestamp>.json
    llm_acceptance_report_<timestamp>.md
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests

DEFAULT_CREDENTIALS_FILE = "tenant_fixtures.json"


@dataclass
class CycleOutcome:
    tenant_id: str
    use_llm: bool
    http_status: int
    latency_ms: float
    cycle_status: str | None = None
    actions_attempted: int | None = None
    llm_section: dict | None = None
    llm_calls: int | None = None
    llm_failures: int | None = None
    error: str | None = None


@dataclass
class Report:
    base_url: str
    started_at: str
    readiness_status: int
    finished_at: str | None = None
    outcomes: list[CycleOutcome] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "base_url": self.base_url,
            "readiness_status": self.readiness_status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "outcomes": [vars(o) for o in self.outcomes],
        }


def _load_credentials(path: Path) -> dict[str, str]:
    with path.open() as f:
        creds = json.load(f)
    if not isinstance(creds, dict) or not creds:
        raise RuntimeError(f"{path} must be a non-empty tenant fixture or credentials object")

    normalized: dict[str, str] = {}
    for tenant_id, value in creds.items():
        api_key = value.get("api_key") if isinstance(value, dict) else value
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise RuntimeError(f"{path} contains an invalid tenant ID")
        if not isinstance(api_key, str) or not api_key.strip():
            raise RuntimeError(f"{path} has no valid API key for tenant {tenant_id}")
        normalized[tenant_id] = api_key
    return normalized


def _select_credentials(
    credentials: dict[str, str],
    tenant_id: str | None,
) -> dict[str, str]:
    if tenant_id is None:
        return credentials
    if tenant_id not in credentials:
        raise ValueError(
            f"tenant {tenant_id!r} is not present in the credentials file"
        )
    return {tenant_id: credentials[tenant_id]}


def run_cycle(
    base_url: str,
    tenant_id: str,
    api_key: str,
    *,
    use_llm: bool,
    timeout_s: int,
    mode: str = "simulate",
) -> CycleOutcome:
    started = time.monotonic()
    try:
        resp = requests.post(
            f"{base_url}/v1/cycles",
            json={
                "mode": mode,
                "use_llm": use_llm,
                "max_opportunities": 10,
                "max_actions": 3,
            },
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_s,
        )
    except requests.RequestException as exc:
        return CycleOutcome(
            tenant_id=tenant_id, use_llm=use_llm, http_status=0,
            latency_ms=round((time.monotonic() - started) * 1000, 1),
            error=f"request failed: {exc}",
        )

    latency_ms = round((time.monotonic() - started) * 1000, 1)

    if resp.status_code != 200:
        return CycleOutcome(
            tenant_id=tenant_id, use_llm=use_llm, http_status=resp.status_code,
            latency_ms=latency_ms, error=resp.text[:500],
        )

    try:
        body = resp.json()
    except requests.exceptions.JSONDecodeError:
        return CycleOutcome(
            tenant_id=tenant_id,
            use_llm=use_llm,
            http_status=200,
            latency_ms=latency_ms,
            error="API returned a non-JSON success response",
        )
    if not isinstance(body, dict):
        return CycleOutcome(
            tenant_id=tenant_id,
            use_llm=use_llm,
            http_status=200,
            latency_ms=latency_ms,
            error="API returned an unexpected response shape",
        )

    llm_section = body.get("llm")
    llm_calls = llm_section.get("calls") if isinstance(llm_section, dict) else None
    llm_failures = llm_section.get("failures") if isinstance(llm_section, dict) else None
    error = None
    if use_llm:
        if not isinstance(llm_section, dict) or not isinstance(llm_calls, int):
            error = "API response did not report LLM invocation metrics"
        elif llm_calls == 0:
            error = "No Bedrock call was made; this tenant produced no LLM-eligible opportunity"
        elif llm_failures != 0:
            error = f"Bedrock reasoning reported {llm_failures} failure(s)"

    return CycleOutcome(
        tenant_id=tenant_id, use_llm=use_llm, http_status=200, latency_ms=latency_ms,
        cycle_status=body.get("status"), actions_attempted=body.get("actions_attempted"),
        llm_section=llm_section, llm_calls=llm_calls, llm_failures=llm_failures,
        error=error,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("OPS_COPILOT_BASE_URL"),
        help="deployed API URL (or set OPS_COPILOT_BASE_URL)",
    )
    parser.add_argument(
        "--credentials-file",
        default=os.environ.get("OPS_COPILOT_CREDENTIALS_FILE", DEFAULT_CREDENTIALS_FILE),
        help="tenant fixture JSON or {tenant_id: api_key} JSON",
    )
    parser.add_argument(
        "--tenant-id",
        help="run the deterministic and LLM cycles for exactly this fixture tenant",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.environ.get("OPS_COPILOT_CYCLE_TIMEOUT_S", "120")),
        help="per-request timeout in seconds",
    )
    args = parser.parse_args(argv)
    if not args.base_url:
        parser.error("--base-url or OPS_COPILOT_BASE_URL is required; localhost is not assumed")
    parsed_url = urlsplit(args.base_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        parser.error("--base-url must be an absolute HTTP or HTTPS URL")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    base_url = args.base_url.rstrip("/")

    try:
        credentials = _load_credentials(Path(args.credentials_file))
    except (OSError, json.JSONDecodeError, RuntimeError) as exc:
        parser.error(str(exc))
    try:
        credentials = _select_credentials(credentials, args.tenant_id)
    except ValueError as exc:
        parser.error(str(exc))

    print(f"Target: {base_url}")
    print(f"Tenant fixture: {args.credentials_file}")
    print(f"Tenants: {len(credentials)}")
    try:
        readiness = requests.get(f"{base_url}/readyz", timeout=args.timeout)
    except requests.RequestException as exc:
        print(f"Readiness check failed before any cycles were sent: {exc}")
        return 1
    try:
        ready_body = readiness.json()
    except requests.exceptions.JSONDecodeError:
        ready_body = {}
    if readiness.status_code != 200 or not isinstance(ready_body, dict) or ready_body.get("status") != "ready":
        print(
            "Readiness check failed before any cycles were sent: "
            f"HTTP {readiness.status_code}"
        )
        return 1
    print("Readiness: PASS")

    report = Report(
        base_url=base_url,
        started_at=datetime.now(timezone.utc).isoformat(),
        readiness_status=readiness.status_code,
    )

    for tenant_id, api_key in credentials.items():
        print(f"\n-- tenant {tenant_id} --")

        # Baseline: deterministic-only, to compare against the LLM run and
        # confirm the LLM path isn't the *only* thing producing candidates
        # (i.e. the non-negotiable fallback really is reachable on its own).
        baseline = run_cycle(
            base_url, tenant_id, api_key, use_llm=False, timeout_s=args.timeout,
        )
        report.outcomes.append(baseline)
        print(f"  use_llm=False -> status={baseline.http_status} cycle_status={baseline.cycle_status} "
              f"actions={baseline.actions_attempted} ({baseline.latency_ms} ms)")

        # The actual LLM-backed run against real Bedrock.
        with_llm = run_cycle(
            base_url, tenant_id, api_key, use_llm=True, timeout_s=args.timeout,
        )
        report.outcomes.append(with_llm)
        print(f"  use_llm=True  -> status={with_llm.http_status} cycle_status={with_llm.cycle_status} "
              f"actions={with_llm.actions_attempted} llm_calls={with_llm.llm_calls} "
              f"llm_failures={with_llm.llm_failures} ({with_llm.latency_ms} ms)")
        if with_llm.llm_section is not None:
            print(f"  llm section: {json.dumps(with_llm.llm_section, default=str)}")
        if with_llm.error:
            print(f"  ERROR: {with_llm.error}")

    report.finished_at = datetime.now(timezone.utc).isoformat()

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = f"llm_acceptance_report_{ts}.json"
    md_path = f"llm_acceptance_report_{ts}.md"

    with open(json_path, "w") as f:
        json.dump(report.to_dict(), f, indent=2, default=str)

    _write_markdown(report, md_path)

    print(f"\nWrote {json_path}")
    print(f"Wrote {md_path}")

    failures = [o for o in report.outcomes if o.http_status != 200 or o.error]
    if failures:
        print(f"\n{len(failures)} cycle(s) did not complete cleanly -- see report for detail.")
        return 1

    return 0


def _write_markdown(report: Report, path: str) -> None:
    llm_outcomes = [o for o in report.outcomes if o.use_llm]
    det_outcomes = [o for o in report.outcomes if not o.use_llm]
    llm_latencies = [o.latency_ms for o in llm_outcomes if o.http_status == 200]
    det_latencies = [o.latency_ms for o in det_outcomes if o.http_status == 200]

    lines = [
        "# LLM-based post-deployment acceptance report",
        "",
        f"- Target: `{report.base_url}`",
        f"- Readiness: HTTP {report.readiness_status}",
        f"- Started: {report.started_at}",
        f"- Finished: {report.finished_at}",
        f"- Tenants exercised: {len({o.tenant_id for o in report.outcomes})}",
        "",
        "## Summary",
        "",
        "| Mode | Cycles | HTTP 200 | Errors | LLM calls | LLM failures | Mean latency (ms) |",
        "|---|---|---|---|---|---|---|",
        f"| use_llm=False | {len(det_outcomes)} | {sum(o.http_status == 200 for o in det_outcomes)} "
        f"| {sum(bool(o.error) for o in det_outcomes)} "
        f"| {sum(o.llm_calls or 0 for o in det_outcomes)} "
        f"| {sum(o.llm_failures or 0 for o in det_outcomes)} "
        f"| {round(statistics.mean(det_latencies), 1) if det_latencies else 'n/a'} |",
        f"| use_llm=True | {len(llm_outcomes)} | {sum(o.http_status == 200 for o in llm_outcomes)} "
        f"| {sum(bool(o.error) for o in llm_outcomes)} "
        f"| {sum(o.llm_calls or 0 for o in llm_outcomes)} "
        f"| {sum(o.llm_failures or 0 for o in llm_outcomes)} "
        f"| {round(statistics.mean(llm_latencies), 1) if llm_latencies else 'n/a'} |",
        "",
        "## Per-tenant detail",
        "",
        "| Tenant | Mode | HTTP | Cycle status | Actions attempted | LLM calls | LLM failures | Latency (ms) | Error |",
        "|---|---|---|---|---|---|---|---|---|",
    ]

    for o in report.outcomes:
        lines.append(
            f"| {o.tenant_id} | {'llm' if o.use_llm else 'deterministic'} | {o.http_status} "
            f"| {o.cycle_status or ''} | {o.actions_attempted if o.actions_attempted is not None else ''} "
            f"| {o.llm_calls if o.llm_calls is not None else ''} "
            f"| {o.llm_failures if o.llm_failures is not None else ''} "
            f"| {o.latency_ms} | {(o.error or '').replace(chr(10), ' ')[:200]} |"
        )

    lines += [
        "",
        "## Notes",
        "",
        "- `use_llm=False` cycles are the deterministic-reasoning baseline -- they must succeed "
        "independent of Bedrock availability (this is the non-negotiable fallback Component 3's "
        "architecture is built around; see orchestrator.py).",
        "- A `use_llm=True` cycle passes only when the API reports one or more Bedrock calls "
        "and zero LLM failures; this prevents deterministic fallback from being reported as "
        "a successful Bedrock test.",
        "- `actions_attempted` reflects whatever your real seeded tenant data produced this run; "
        "it is expected to vary run-to-run and is not itself a pass/fail signal.",
    ]

    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def test_load_credentials_accepts_bootstrap_fixtures_and_credentials(tmp_path):
    fixtures = tmp_path / "tenant_fixtures.json"
    fixtures.write_text(json.dumps({
        "tenant-1": {"api_key": "fixture-key", "menu_item_id": "item-1"},
        "tenant-2": "legacy-key",
    }))

    assert _load_credentials(fixtures) == {
        "tenant-1": "fixture-key",
        "tenant-2": "legacy-key",
    }


def test_select_credentials_restricts_run_to_requested_tenant():
    credentials = {"tenant-1": "key-1", "tenant-2": "key-2"}

    assert _select_credentials(credentials, "tenant-2") == {
        "tenant-2": "key-2",
    }


def test_select_credentials_rejects_tenant_missing_from_fixture():
    try:
        _select_credentials({"tenant-1": "key-1"}, "tenant-2")
    except ValueError as exc:
        assert "not present in the credentials file" in str(exc)
    else:
        raise AssertionError("missing fixture tenant should be rejected")


def test_llm_cycle_fails_when_api_only_used_fallback(monkeypatch):
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "status": "completed",
                "actions_attempted": 0,
                "llm": {"calls": 0, "failures": 0, "candidates": 0, "errors": []},
            }

    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: Response())

    outcome = run_cycle(
        "http://staging.example",
        "tenant-1",
        "test-key",
        use_llm=True,
        timeout_s=5,
    )

    assert outcome.http_status == 200
    assert outcome.llm_calls == 0
    assert outcome.error == "No Bedrock call was made; this tenant produced no LLM-eligible opportunity"


def test_llm_cycle_passes_with_real_llm_activity(monkeypatch):
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "status": "completed",
                "actions_attempted": 0,
                "llm": {"calls": 2, "failures": 0, "candidates": 1, "errors": []},
            }

    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: Response())

    outcome = run_cycle(
        "http://staging.example",
        "tenant-1",
        "test-key",
        use_llm=True,
        timeout_s=5,
    )

    assert outcome.error is None
    assert outcome.llm_calls == 2
    assert outcome.llm_failures == 0


if __name__ == "__main__":
    raise SystemExit(main())