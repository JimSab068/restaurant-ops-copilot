"""Post-deployment acceptance runner for the Restaurant Ops Copilot.

This is intentionally an executable acceptance runner rather than a pytest
module: it talks only to a running API/worker pair and therefore works against
the Terraform ALB and a locally run Docker image equally well.  It never seeds
or cleans a database.

Examples:

    # Terraform / ECS (the default target is supplied explicitly)
    OPS_COPILOT_BASE_URL=http://$(terraform -chdir=terraform output -raw alb_dns_name) \
    OPS_COPILOT_TENANT_FIXTURES=tenant_fixtures.json \
    python tests/live_tests/deployed_system_acceptance.py

    # Docker image already running with its API and worker dependencies
    OPS_COPILOT_TARGET=local OPS_COPILOT_TENANT_FIXTURES=tenant_fixtures.json \
    python tests/live_tests/deployed_system_acceptance.py --quick

``tenant_fixtures.json`` is the file emitted by ``python seed.py``.  For this
runner it must contain at least ten entries and each entry needs ``api_key``,
``menu_item_id``, ``ingredient_id``, and ``ingredient_price``. API keys are
never written to the report.

Optional ``OPS_COPILOT_RELEASE_GATE_DATABASE_URL`` enables the final read-only
SQL release gates.  Use a dedicated, read-only operational-audit credential;
an ordinary tenant-RLS application URL cannot inspect every tenant.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import statistics
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


DEFAULT_LOCAL_URL = "http://localhost:8000"


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""
    latency_ms: float | None = None
    skipped: bool = False
    unverified: bool = False


@dataclass
class Report:
    target: str
    started_at: str
    checks: list[Check] = field(default_factory=list)
    finished_at: str | None = None

    def add(self, name: str, passed: bool, detail: str = "", latency_ms: float | None = None) -> bool:
        self.checks.append(Check(name, passed, detail, latency_ms))
        print(("PASS" if passed else "FAIL"), name, (f"-- {detail}" if detail else ""))
        return passed

    def skip(self, name: str, detail: str) -> None:
        self.checks.append(Check(name, False, detail, skipped=True))
        print("SKIP", name, f"-- {detail}")

    def unverified(self, name: str, detail: str) -> None:
        """Record an unknown result without treating it as a passing gate."""
        self.checks.append(Check(name, False, detail, unverified=True))
        print("UNVERIFIED", name, f"-- {detail}")


class AcceptanceRun:
    def __init__(self, base_url: str, tenants: dict[str, dict[str, Any]], report: Report, timeout: int, poll_timeout: int):
        self.base_url = base_url.rstrip("/")
        self.tenants = list(tenants.items())
        self.report = report
        self.timeout = timeout
        self.poll_timeout = poll_timeout

    @staticmethod
    def _headers(tenant: dict[str, Any]) -> dict[str, str]:
        return {"Authorization": f"Bearer {tenant['api_key']}"}

    def submit(self, tenant: dict[str, Any], action_type: str, payload: dict[str, Any], key: str | None = None) -> requests.Response:
        return requests.post(
            f"{self.base_url}/v1/actions", headers=self._headers(tenant), timeout=self.timeout,
            json={"idempotency_key": key or f"acceptance-{uuid.uuid4()}", "action_type": action_type, "payload": payload},
        )

    def status(self, tenant: dict[str, Any], request_id: str) -> tuple[dict[str, Any] | None, str | None]:
        deadline = time.monotonic() + self.poll_timeout
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            response = requests.get(f"{self.base_url}/v1/actions/{request_id}", headers=self._headers(tenant), timeout=self.timeout)
            if response.status_code != 200:
                return None, f"GET returned {response.status_code}: {response.text[:200]}"
            last = response.json()
            if last["status"] in {"completed", "failed"}:
                return last, None
            time.sleep(1)
        return last, f"request remained {last and last.get('status')!r} after {self.poll_timeout}s"

    def action(self, label: str, tenant: dict[str, Any], action_type: str, payload: dict[str, Any], expected_reason: str | None, *, key: str | None = None) -> dict[str, Any] | None:
        started = time.monotonic()
        try:
            response = self.submit(tenant, action_type, payload, key)
            if response.status_code != 202:
                self.report.add(label, False, f"POST returned {response.status_code}: {response.text[:300]}")
                return None
            row, error = self.status(tenant, response.json()["request_id"])
        except requests.RequestException as exc:
            self.report.add(label, False, f"HTTP error: {exc}")
            return None
        elapsed = round((time.monotonic() - started) * 1000, 1)
        if error:
            self.report.add(label, False, error, elapsed)
            return row
        row_error = row.get("error") or {}
        actual_reason = row_error.get("reason") or row_error.get("error")
        passed = (row["status"] == "completed" if expected_reason is None else actual_reason == expected_reason)
        self.report.add(label, passed, f"status={row['status']} reason={actual_reason!r}", elapsed)
        return row

    def health(self) -> None:
        for route, expected in (("healthz", "ok"), ("readyz", "ready")):
            try:
                response = requests.get(f"{self.base_url}/{route}", timeout=self.timeout)
                self.report.add(f"{route}", response.status_code == 200 and response.json().get("status") == expected,
                                f"HTTP {response.status_code}")
            except requests.RequestException as exc:
                self.report.add(f"{route}", False, str(exc))

    def catalog(self) -> None:
        live_prices: dict[str, float] = {}
        for index, (tenant_id, tenant) in enumerate(self.tenants):
            try:
                response = requests.get(
                    f"{self.base_url}/v1/menu-items/{tenant['menu_item_id']}",
                    headers=self._headers(tenant),
                    timeout=self.timeout,
                )
                if response.status_code != 200:
                    raise ValueError(
                        f"GET returned {response.status_code}: {response.text[:200]}"
                    )
                body = response.json()
                if not isinstance(body, dict):
                    raise ValueError("API returned a non-object response")
                if body.get("menu_item_id") != tenant["menu_item_id"]:
                    raise ValueError("API returned a different menu item")
                price = float(body["current_price"])
                if not math.isfinite(price) or price <= 0:
                    raise ValueError("API returned an invalid current price")
            except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
                self.report.add(
                    f"tenant-{index}: retrieve live menu price",
                    False,
                    str(exc),
                )
                continue

            live_prices[tenant_id] = price
            self.report.add(
                f"tenant-{index}: retrieve live menu price",
                True,
                f"current_price={price:.2f}",
            )

        if len(live_prices) != len(self.tenants):
            return

        # Base each probe on the last price known to have executed. A held
        # action does not change the menu price and must not affect later
        # thresholds.
        for index, (tenant_id, tenant) in enumerate(self.tenants):
            price = live_prices[tenant_id]
            for pct in (0.01, 0.03, 0.05, 0.07, 0.09):
                new_price = round(price * (1 + pct), 2)
                outcome = self.action(
                    f"tenant-{index}: autonomous price {pct:.0%}",
                    tenant,
                    "price_change",
                    {"menu_item_id": tenant["menu_item_id"], "new_price": new_price},
                    None,
                )
                if outcome is not None and outcome.get("status") == "completed":
                    price = new_price
            for pct in (0.15, 0.20, 0.25):
                self.action(f"tenant-{index}: approval price {pct:.0%}", tenant, "price_change",
                            {"menu_item_id": tenant["menu_item_id"], "new_price": round(price * (1 + pct), 2)},
                            "pending_human_approval")
            self.action(f"tenant-{index}: critic limit", tenant, "price_change",
                        {"menu_item_id": tenant["menu_item_id"], "new_price": round(price * 1.32, 2)}, "blocked_by_critic")
        # Current simulator confidence intentionally makes these two paths
        # held_low_confidence.  A policy change should update these assertions.
        _, tenant = self.tenants[0]
        self.action("supplier switch requires review", tenant, "supplier_switch",
                    {"ingredient_id": tenant["ingredient_id"], "new_supplier_price": round(float(tenant["ingredient_price"]) * 1.05, 2)},
                    "pending_human_approval")
        self.action("staffing remains gated", tenant, "staffing_change", {"headcount_delta": 1}, "held_low_confidence")
        self.action("menu removal remains gated", tenant, "menu_swap",
                    {"menu_item_id": tenant["menu_item_id"], "action": "remove"}, "held_low_confidence")

    def isolation_and_idempotency(self) -> None:
        _, first = self.tenants[0]
        other_id, other = self.tenants[1]
        payload = {"headcount_delta": 1}
        key = f"sequential-{uuid.uuid4()}"
        a, b = self.submit(first, "staffing_change", payload, key), self.submit(first, "staffing_change", payload, key)
        same = a.status_code == b.status_code == 202 and a.json().get("request_id") == b.json().get("request_id")
        self.report.add("sequential idempotency", same)
        if same:
            self.status(first, a.json()["request_id"])

        race_key = f"race-{uuid.uuid4()}"
        def duplicate() -> requests.Response:
            return self.submit(first, "staffing_change", payload, race_key)
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            replies = list(pool.map(lambda _: duplicate(), range(10)))
        ids = {r.json().get("request_id") for r in replies if r.status_code == 202}
        self.report.add("concurrent idempotency", len(ids) == 1 and len(replies) == 10,
                        f"accepted={sum(r.status_code == 202 for r in replies)} distinct_request_ids={len(ids)}")

        # The cross-tenant ID must never be usable under the first tenant key.
        foreign = self.action("cross-tenant menu reference fails closed", first, "price_change",
                              {"menu_item_id": other["menu_item_id"], "new_price": 13.0}, "tool_framework_error")
        if foreign is not None and foreign.get("status") == "processing":
            # This is a release failure, not an acceptable alternate outcome.
            self.report.add("cross-tenant request is not stuck", False, "invalid cross-tenant request remained processing")

        malformed = self.action("missing new_price fails cleanly", first, "price_change",
                                {"menu_item_id": first["menu_item_id"]}, "tool_framework_error")
        if malformed is not None and malformed.get("status") == "processing":
            self.report.add("malformed request is not stuck", False, "missing new_price remained processing")

        smuggled = requests.post(
            f"{self.base_url}/v1/actions", headers=self._headers(first), timeout=self.timeout,
            json={"idempotency_key": f"smuggle-{uuid.uuid4()}", "action_type": "staffing_change", "payload": payload,
                  "tenant_id": other_id},
        )
        self.report.add("tenant-id smuggling ignored", smuggled.status_code == 202,
                        "authenticated key, not supplied tenant_id, determines tenancy")
        unknown = requests.post(f"{self.base_url}/v1/actions", headers={"Authorization": "Bearer garbage"}, timeout=self.timeout,
                                json={"idempotency_key": str(uuid.uuid4()), "action_type": "price_change", "payload": payload})
        self.report.add("unknown key rejected", unknown.status_code == 401, f"HTTP {unknown.status_code}")
        if smuggled.status_code == 202:
            hidden = requests.get(f"{self.base_url}/v1/actions/{smuggled.json()['request_id']}", headers=self._headers(other), timeout=self.timeout)
            self.report.add("cross-tenant GET is 404", hidden.status_code == 404, f"HTTP {hidden.status_code}")

    def concurrent_load(self, use_llm: bool) -> None:
        # One safe bounded action per tenant at once proves cross-tenant FIFO
        # parallelism without changing an item repeatedly under artificial load.
        def submit_one(pair: tuple[str, dict[str, Any]]) -> tuple[dict[str, Any], requests.Response]:
            _, tenant = pair
            return tenant, self.submit(tenant, "staffing_change", {"headcount_delta": 1}, f"load-{uuid.uuid4()}")
        started = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(self.tenants)) as pool:
            submitted = list(pool.map(submit_one, self.tenants))
        rows = [self.status(tenant, response.json()["request_id"]) for tenant, response in submitted if response.status_code == 202]
        ok = len(submitted) == len(self.tenants) and all(r[0] is not None and r[1] is None for r in rows)
        self.report.add("all-tenant concurrent action load", ok, f"requests={len(submitted)}", round((time.monotonic() - started) * 1000, 1))

        if not use_llm:
            return
        def cycle(pair: tuple[str, dict[str, Any]]) -> tuple[int, dict[str, Any] | None]:
            _, tenant = pair
            response = requests.post(f"{self.base_url}/v1/cycles", headers=self._headers(tenant), timeout=self.timeout,
                                     json={"mode": "simulate", "use_llm": True, "max_opportunities": 10, "max_actions": 3})
            return response.status_code, response.json() if response.headers.get("content-type", "").startswith("application/json") else None
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(self.tenants)) as pool:
            cycles = list(pool.map(cycle, self.tenants))
        self.report.add("all-tenant concurrent LLM cycles", all(code == 200 and body and body.get("status") == "completed" for code, body in cycles),
                        f"HTTP statuses={[code for code, _ in cycles]}")

    def release_gates(
        self,
        database_url: str | None,
        *,
        required: bool,
        skip: bool = False,
    ) -> None:
        if skip:
            self.report.skip(
                "SQL release gates",
                "not run by request; RDS is not directly reachable from the laptop",
            )
            return
        if not database_url:
            self.report.add("SQL release gates", not required,
                            "skipped: set OPS_COPILOT_RELEASE_GATE_DATABASE_URL")
            return
        from sqlalchemy import create_engine, text
        failure_queries = {
            "no duplicate work requests": "SELECT count(*) FROM (SELECT 1 FROM work_requests GROUP BY tenant_id, idempotency_key HAVING count(*) > 1) x",
            "no unsafe autonomous executions": "SELECT count(*) FROM decisions WHERE status = 'EXECUTED' AND action_type IN ('SUPPLIER_SWITCH', 'MENU_SWAP')",
            "no stale processing requests": "SELECT count(*) FROM work_requests WHERE status = 'processing' AND updated_at < now() - interval '5 minutes'",
            "blocked/held outcomes have evaluations with no explicit isolation breach": "SELECT count(*) FROM tool_execution_log l WHERE l.executed = false AND (NOT EXISTS (SELECT 1 FROM evaluation_records e WHERE e.tenant_id = l.tenant_id AND e.decision_id = l.decision_id) OR EXISTS (SELECT 1 FROM evaluation_records e WHERE e.tenant_id = l.tenant_id AND e.decision_id = l.decision_id AND e.isolation_breach IS TRUE))",
        }
        unverified_queries = {
            "blocked/held outcomes with unknown isolation status": "SELECT count(*) FROM tool_execution_log l WHERE l.executed = false AND EXISTS (SELECT 1 FROM evaluation_records e WHERE e.tenant_id = l.tenant_id AND e.decision_id = l.decision_id AND e.isolation_breach IS NULL)",
        }
        try:
            engine = create_engine(database_url)
            with engine.connect() as connection:
                for name, query in failure_queries.items():
                    count = connection.execute(text(query)).scalar_one()
                    self.report.add(f"release gate: {name}", count == 0, f"violations={count}")
                for name, query in unverified_queries.items():
                    count = connection.execute(text(query)).scalar_one()
                    if count:
                        self.report.unverified(
                            f"release gate: {name}",
                            f"records={count}; NULL is unknown, not confirmed safe",
                        )
        except Exception as exc:
            self.report.add("SQL release gates", False, f"query failed: {type(exc).__name__}: {exc}")


def load_fixtures(path: Path, *, minimum_tenants: int) -> dict[str, dict[str, Any]]:
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or len(data) < minimum_tenants:
        raise ValueError(f"fixtures must be a JSON object with at least {minimum_tenants} tenant entries")
    required = {"api_key", "menu_item_id", "ingredient_id", "ingredient_price"}
    missing = {tenant_id: sorted(required - set(value)) for tenant_id, value in data.items() if not isinstance(value, dict) or required - set(value)}
    if missing:
        raise ValueError(f"fixtures are missing required values: {missing}")
    return data


def write_report(report: Report, directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path, md_path = directory / f"deployed_acceptance_{stamp}.json", directory / f"deployed_acceptance_{stamp}.md"
    json_path.write_text(json.dumps({"target": report.target, "started_at": report.started_at, "finished_at": report.finished_at,
                                     "checks": [asdict(c) for c in report.checks]}, indent=2) + "\n")
    lines = ["# Deployed-system acceptance report", "", f"- Target: `{report.target}`", f"- Started: {report.started_at}", f"- Finished: {report.finished_at}", "", "| Check | Result | Detail |", "|---|---|---|"]
    for check in report.checks:
        escaped_detail = check.detail.replace("|", "\\|")
        result = (
            "SKIP" if check.skipped else
            "UNVERIFIED" if check.unverified else
            "PASS" if check.passed else "FAIL"
        )
        lines.append(f"| {check.name} | {result} | {escaped_detail} |")
    md_path.write_text("\n".join(lines) + "\n")
    return json_path, md_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("terraform", "local"), default=os.getenv("OPS_COPILOT_TARGET", "terraform"))
    parser.add_argument("--base-url", default=os.getenv("OPS_COPILOT_BASE_URL"))
    parser.add_argument("--fixtures", default=os.getenv("OPS_COPILOT_TENANT_FIXTURES", "tenant_fixtures.json"))
    parser.add_argument("--quick", action="store_true", help="skip catalog and LLM cycles; retain health, isolation, and concurrency checks")
    parser.add_argument(
        "--skip-sql-release-gates",
        action="store_true",
        help="record private-RDS SQL release gates as skipped; run the HTTP acceptance checks normally",
    )
    parser.add_argument("--with-llm", action="store_true", default=os.getenv("OPS_COPILOT_WITH_LLM") == "1")
    parser.add_argument("--timeout", type=int, default=int(os.getenv("OPS_COPILOT_HTTP_TIMEOUT_S", "60")))
    parser.add_argument("--poll-timeout", type=int, default=int(os.getenv("OPS_COPILOT_POLL_TIMEOUT_S", "180")))
    parser.add_argument("--report-dir", default=os.getenv("OPS_COPILOT_REPORT_DIR", "artifacts"))
    args = parser.parse_args()
    base_url = args.base_url or (DEFAULT_LOCAL_URL if args.target == "local" else "")
    if not base_url:
        parser.error("--base-url or OPS_COPILOT_BASE_URL is required for --target terraform")
    try:
        tenants = load_fixtures(Path(args.fixtures), minimum_tenants=2 if args.quick else 10)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    report = Report(base_url, datetime.now(timezone.utc).isoformat())
    run = AcceptanceRun(base_url, tenants, report, args.timeout, args.poll_timeout)
    run.health()
    if not args.quick:
        run.catalog()
    run.isolation_and_idempotency()
    run.concurrent_load(args.with_llm and not args.quick)
    run.release_gates(
        os.getenv("OPS_COPILOT_RELEASE_GATE_DATABASE_URL"),
        required=args.target == "terraform" and not args.quick,
        skip=args.skip_sql_release_gates,
    )
    report.finished_at = datetime.now(timezone.utc).isoformat()
    json_path, md_path = write_report(report, Path(args.report_dir))
    failures = [
        check for check in report.checks
        if not check.passed and not check.skipped and not check.unverified
    ]
    print(f"\nWrote {json_path} and {md_path}; {len(failures)} failure(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
