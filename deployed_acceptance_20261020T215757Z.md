# Deployed-system acceptance report

- Started: 2026-10-20T21:57:08.809201+00:00
- Finished: 2026-10-20T21:57:57.482026+00:00

| Check | Result | Detail |
|---|---|---|
| healthz | PASS | HTTP 200 |
| readyz | PASS | HTTP 200 |
| tenant-0: retrieve live menu price | PASS | current_price=32.89 |
| tenant-1: retrieve live menu price | PASS | current_price=27.84 |
| tenant-2: retrieve live menu price | PASS | current_price=20.26 |
| tenant-3: retrieve live menu price | PASS | current_price=25.32 |
| tenant-4: retrieve live menu price | PASS | current_price=60.03 |
| tenant-5: retrieve live menu price | PASS | current_price=40.92 |
| tenant-6: retrieve live menu price | PASS | current_price=12.28 |
| tenant-7: retrieve live menu price | PASS | current_price=9.67 |
| tenant-8: retrieve live menu price | PASS | current_price=24.56 |
| tenant-9: retrieve live menu price | PASS | current_price=45.58 |
| tenant-0: autonomous price 1% | PASS | status=completed reason=None |
| tenant-0: autonomous price 3% | PASS | status=completed reason=None |
| tenant-0: autonomous price 5% | PASS | status=completed reason=None |
| tenant-0: autonomous price 7% | PASS | status=completed reason=None |
| tenant-0: autonomous price 9% | PASS | status=completed reason=None |
| tenant-0: approval price 15% | PASS | status=failed reason='pending_human_approval' |
| tenant-0: approval price 20% | PASS | status=failed reason='pending_human_approval' |
| tenant-0: approval price 25% | PASS | status=failed reason='pending_human_approval' |
| tenant-0: critic limit | PASS | status=failed reason='blocked_by_critic' |
| tenant-1: autonomous price 1% | PASS | status=completed reason=None |
| tenant-1: autonomous price 3% | PASS | status=completed reason=None |
| tenant-1: autonomous price 5% | PASS | status=completed reason=None |
| tenant-1: autonomous price 7% | PASS | status=completed reason=None |
| tenant-1: autonomous price 9% | PASS | status=completed reason=None |
| tenant-1: approval price 15% | PASS | status=failed reason='pending_human_approval' |
| tenant-1: approval price 20% | PASS | status=failed reason='pending_human_approval' |
| tenant-1: approval price 25% | PASS | status=failed reason='pending_human_approval' |
| tenant-1: critic limit | PASS | status=failed reason='blocked_by_critic' |
| tenant-2: autonomous price 1% | PASS | status=completed reason=None |
| tenant-2: autonomous price 3% | PASS | status=completed reason=None |
| tenant-2: autonomous price 5% | PASS | status=completed reason=None |
| tenant-2: autonomous price 7% | PASS | status=completed reason=None |
| tenant-2: autonomous price 9% | PASS | status=completed reason=None |
| tenant-2: approval price 15% | PASS | status=failed reason='pending_human_approval' |
| tenant-2: approval price 20% | PASS | status=failed reason='pending_human_approval' |
| tenant-2: approval price 25% | PASS | status=failed reason='pending_human_approval' |
| tenant-2: critic limit | PASS | status=failed reason='blocked_by_critic' |
| tenant-3: autonomous price 1% | PASS | status=completed reason=None |
| tenant-3: autonomous price 3% | PASS | status=completed reason=None |
| tenant-3: autonomous price 5% | PASS | status=completed reason=None |
| tenant-3: autonomous price 7% | PASS | status=completed reason=None |
| tenant-3: autonomous price 9% | PASS | status=completed reason=None |
| tenant-3: approval price 15% | PASS | status=failed reason='pending_human_approval' |
| tenant-3: approval price 20% | PASS | status=failed reason='pending_human_approval' |
| tenant-3: approval price 25% | PASS | status=failed reason='pending_human_approval' |
| tenant-3: critic limit | PASS | status=failed reason='blocked_by_critic' |
| tenant-4: autonomous price 1% | PASS | status=completed reason=None |
| tenant-4: autonomous price 3% | PASS | status=completed reason=None |
| tenant-4: autonomous price 5% | PASS | status=completed reason=None |
| tenant-4: autonomous price 7% | PASS | status=completed reason=None |
| tenant-4: autonomous price 9% | PASS | status=completed reason=None |
| tenant-4: approval price 15% | PASS | status=failed reason='pending_human_approval' |
| tenant-4: approval price 20% | PASS | status=failed reason='pending_human_approval' |
| tenant-4: approval price 25% | PASS | status=failed reason='pending_human_approval' |
| tenant-4: critic limit | PASS | status=failed reason='blocked_by_critic' |
| tenant-5: autonomous price 1% | PASS | status=completed reason=None |
| tenant-5: autonomous price 3% | PASS | status=completed reason=None |
| tenant-5: autonomous price 5% | PASS | status=completed reason=None |
| tenant-5: autonomous price 7% | PASS | status=completed reason=None |
| tenant-5: autonomous price 9% | PASS | status=completed reason=None |
| tenant-5: approval price 15% | PASS | status=failed reason='pending_human_approval' |
| tenant-5: approval price 20% | PASS | status=failed reason='pending_human_approval' |
| tenant-5: approval price 25% | PASS | status=failed reason='pending_human_approval' |
| tenant-5: critic limit | PASS | status=failed reason='blocked_by_critic' |
| tenant-6: autonomous price 1% | PASS | status=completed reason=None |
| tenant-6: autonomous price 3% | PASS | status=completed reason=None |
| tenant-6: autonomous price 5% | PASS | status=completed reason=None |
| tenant-6: autonomous price 7% | PASS | status=completed reason=None |
| tenant-6: autonomous price 9% | PASS | status=completed reason=None |
| tenant-6: approval price 15% | PASS | status=failed reason='pending_human_approval' |
| tenant-6: approval price 20% | PASS | status=failed reason='pending_human_approval' |
| tenant-6: approval price 25% | PASS | status=failed reason='pending_human_approval' |
| tenant-6: critic limit | PASS | status=failed reason='blocked_by_critic' |
| tenant-7: autonomous price 1% | PASS | status=completed reason=None |
| tenant-7: autonomous price 3% | PASS | status=completed reason=None |
| tenant-7: autonomous price 5% | PASS | status=completed reason=None |
| tenant-7: autonomous price 7% | PASS | status=completed reason=None |
| tenant-7: autonomous price 9% | PASS | status=completed reason=None |
| tenant-7: approval price 15% | PASS | status=failed reason='pending_human_approval' |
| tenant-7: approval price 20% | PASS | status=failed reason='pending_human_approval' |
| tenant-7: approval price 25% | PASS | status=failed reason='pending_human_approval' |
| tenant-7: critic limit | PASS | status=failed reason='blocked_by_critic' |
| tenant-8: autonomous price 1% | PASS | status=completed reason=None |
| tenant-8: autonomous price 3% | PASS | status=completed reason=None |
| tenant-8: autonomous price 5% | PASS | status=completed reason=None |
| tenant-8: autonomous price 7% | PASS | status=completed reason=None |
| tenant-8: autonomous price 9% | PASS | status=completed reason=None |
| tenant-8: approval price 15% | PASS | status=failed reason='pending_human_approval' |
| tenant-8: approval price 20% | PASS | status=failed reason='pending_human_approval' |
| tenant-8: approval price 25% | PASS | status=failed reason='pending_human_approval' |
| tenant-8: critic limit | PASS | status=failed reason='blocked_by_critic' |
| tenant-9: autonomous price 1% | PASS | status=completed reason=None |
| tenant-9: autonomous price 3% | PASS | status=completed reason=None |
| tenant-9: autonomous price 5% | PASS | status=completed reason=None |
| tenant-9: autonomous price 7% | PASS | status=completed reason=None |
| tenant-9: autonomous price 9% | PASS | status=completed reason=None |
| tenant-9: approval price 15% | PASS | status=failed reason='pending_human_approval' |
| tenant-9: approval price 20% | PASS | status=failed reason='pending_human_approval' |
| tenant-9: approval price 25% | PASS | status=failed reason='pending_human_approval' |
| tenant-9: critic limit | PASS | status=failed reason='blocked_by_critic' |
| supplier switch requires review | PASS | status=failed reason='pending_human_approval' |
| staffing remains gated | PASS | status=failed reason='held_low_confidence' |
| menu removal remains gated | PASS | status=failed reason='held_low_confidence' |
| sequential idempotency | PASS |  |
| concurrent idempotency | PASS | accepted=10 distinct_request_ids=1 |
| cross-tenant menu reference fails closed | PASS | status=failed reason='tool_framework_error' |
| missing new_price fails cleanly | PASS | status=failed reason='tool_framework_error' |
| tenant-id smuggling ignored | PASS | authenticated key, not supplied tenant_id, determines tenancy |
| unknown key rejected | PASS | HTTP 401 |
| cross-tenant GET is 404 | PASS | HTTP 404 |
| all-tenant concurrent action load | PASS | requests=10 |
| SQL release gates | SKIP | not run by request; RDS is not directly reachable from the laptop |
