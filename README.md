# Restaurant Ops Copilot

Restaurant Ops Copilot is a multi-tenant operational intelligence system for
restaurants. It turns operational data into constrained, auditable actions:
it observes restaurant state, detects opportunities, reasons about candidate
actions, simulates their impact, applies safety policy, executes permitted
work asynchronously, and records the outcome for evaluation.


## What it does

```text
Operational events and restaurant state
                 ↓
     Opportunity detection and reasoning
                 ↓
     Simulation, confidence, and critic
                 ↓
       Policy: autonomous / approval / hold
                 ↓
       SQS work request → ECS worker → tools
                 ↓
       Evaluation records and calibration feedback
```

The currently implemented action surface includes pricing, procurement,
staffing, and menu actions. The policy boundary permits only appropriately
low-risk, sufficiently confident actions to execute autonomously; higher-risk
or uncertain actions are held or require human approval.

## Architecture

| Layer | Responsibility |
|---|---|
| Component 1 | Tenant-scoped PostgreSQL operational model, immutable events, and point-in-time state reconstruction |
| Component 2 | Economics, demand and stockout simulation, confidence, calibration, and decision support |
| Component 3 | Opportunity detection, deterministic reasoning, and optional Amazon Bedrock-backed LLM reasoning |
| Component 4 | Typed tool framework, action validation, critic integration, execution audit log, and idempotency |
| Component 5 | PostgreSQL row-level security (RLS), tenant context, database roles, and adversarial isolation checks |
| Component 6 | Risk classification, autonomy policy, approval workflow, and safety gates |
| Component 7 | Durable tenant-scoped evaluation records, metrics, and observability |
| API / worker | FastAPI accepts authenticated work; SQS and an ECS worker execute it asynchronously |
| Terraform | AWS networking, RDS, ECR, ECS, ALB, SQS/DLQ, IAM, CloudWatch alarms, bootstrap, and release-gate tasks |

## Security and safety model

- The authenticated API key determines the tenant. Request-body tenant IDs do
  not determine access.
- PostgreSQL RLS is enforced and forced on tenant-scoped tables; the deployed
  cycle endpoint refuses to run unless RLS is verified.
- Cross-tenant resource references fail closed and cross-tenant reads return
  `404`, avoiding resource-existence disclosure.
- Requests use tenant-scoped idempotency keys, including concurrent-submit
  race handling.
- The worker defaults to `SYNTHETIC_EXECUTION_ONLY=true`; it will refuse work
  outside seeded synthetic data.
- High-risk actions remain behind policy/approval boundaries. Every outcome is
  written to an execution log and evaluation ledger.
- The release gates use a dedicated, read-only, non-superuser
  `release_gate_reader` role with forced tenant RLS.

## API

All tenant-facing endpoints require `Authorization: Bearer <tenant-api-key>`.

| Endpoint | Purpose |
|---|---|
| `POST /v1/actions` | Submit an asynchronous, idempotent action request |
| `GET /v1/actions/{request_id}` | Retrieve the tenant-scoped action outcome |
| `GET /v1/menu-items/{menu_item_id}` | Retrieve a current menu-item price |
| `POST /v1/cycles` | Run an operational cycle, optionally using Bedrock reasoning |
| `GET /healthz` | Process liveness check |
| `GET /readyz` | Database-backed readiness check |

## Repository layout

```text
Component_1/       operational model, events, PostgreSQL access
Component_2/       simulation and calibration
Component_3/       detection and deterministic/LLM reasoning
Component_4/       tool execution, validation, idempotency
Component_5/       RLS and database bootstrap
Component_6/       risk, policy, approvals
Component_7/       evaluation and observability
api/               FastAPI application and API-key authentication
worker/            SQS polling and asynchronous execution
shared/            configuration, queue, and structured logging
ops/               read-only SQL release gates
terraform/         AWS infrastructure and ECS task definitions
tests/             unit, integration, and deployed-system acceptance tests
functional_docs/   design and functional documentation
```

## Local development

Prerequisites:

- Python 3.11+ (the production Docker image uses Python 3.11)
- PostgreSQL
- An SQS-compatible queue for API/worker integration, or AWS SQS
- Docker and Terraform only when building or deploying AWS infrastructure

Create an environment and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a dedicated local test database (the helper script provisions
`ops_copilot` and `ops_copilot_test`):

```bash
./scripts/setup_postgres.sh
```

Set configuration outside source control. At minimum, the API/worker need
`DATABASE_URL`, `SQS_QUEUE_URL`, and `API_KEY_PEPPER`; the worker should retain
`SYNTHETIC_EXECUTION_ONLY=true` in development. Do not commit `.env`, tenant
fixtures, database URLs, API keys, Terraform state, or AWS credentials.

Bootstrap the schema, synthetic tenants, roles, and RLS using separately
scoped seed and RLS-admin database URLs:

```bash
SEED_DATABASE_URL='...' RLS_ADMIN_DATABASE_URL='...' ./bootstrap_database.sh
```

Run the API and worker in separate terminals:

```bash
uvicorn api.main:app --host 0.0.0.0 --port 8000
python -m worker.main
```

The root `Dockerfile` builds one image used by both the API and worker; the
ECS task definition chooses the process command.

## Tests

Tests require a dedicated database URL and will refuse to start without it:

```bash
export TEST_DATABASE_URL='postgresql://.../ops_copilot_test'
pytest tests/unit -q
pytest tests/integration -q
```

The test suite includes unit tests, integration tests, RLS isolation tests,
idempotency/concurrency tests, pipeline tests, worker tests, and optional live
LLM tests. Live LLM tests are marked `live_llm` and require real cloud
credentials/configuration.

Run post-deployment acceptance against an API/worker pair using protected
synthetic tenant fixtures:

```bash
OPS_COPILOT_BASE_URL='http://your-alb-or-api' \
OPS_COPILOT_TENANT_FIXTURES=tenant_fixtures.json \
python tests/live_tests/deployed_system_acceptance.py
```

To exercise optional Bedrock reasoning, add `--with-llm`. The acceptance runner
writes timestamped reports to `artifacts/`; keep credentials out of those
reports.

## AWS deployment

Terraform under `terraform/` provisions the AWS runtime: VPC/networking,
PostgreSQL RDS, ECR, ECS API and worker services, ALB, SQS plus DLQ, IAM,
CloudWatch logs/alarms, and one-off bootstrap/release-gate tasks.

At a high level:

```bash
# Build and push the shared application image to the Terraform-created ECR repo.
docker build -t <ecr-repository>:<tag> .
docker push <ecr-repository>:<tag>

# Review before applying. Supply sensitive values through your secure shell/CI.
terraform -chdir=terraform init
terraform -chdir=terraform plan
terraform -chdir=terraform apply
```

After provisioning, run the bootstrap ECS task once. It creates/verifies
application roles, applies the schema and RLS policies, seeds synthetic data,
and rotates acceptance fixtures. Consult [`terraform/outputs.tf`](terraform/outputs.tf)
for the bootstrap, fixture, and release-gate outputs. Never place production
credentials in Terraform variables, image layers, source files, or GitHub.

## Verification status

The latest recorded deployed acceptance run (2026-10-20) passed all
application-level checks across ten seeded tenants: API health/readiness,
autonomous pricing within policy limits, approval/critic holds, idempotency,
concurrent submission behavior, malformed-input handling, and cross-tenant
isolation behavior. A separate Bedrock smoke test also completed successfully
with one real LLM call and no LLM failures.

The project is not claiming release-ready status yet. The separate SQL
release-gate task identified two remaining operational issues:

1. Two work requests were stale in `processing`, demonstrating that abandoned
   transient worker failures still need explicit recovery/retry ownership.
2. Some blocked/held evaluation records have an unknown (`NULL`) isolation
   status. Unknown is deliberately not treated as proof of safe isolation and
   needs explicit telemetry or migration handling.

The old PostgreSQL enum issue affecting supplier-switch execution was resolved
in the later deployment. See `functional_docs/version2.md` for the detailed
product design; its implementation-status section is a historical baseline and
does not yet reflect every subsequently deployed capability.


