<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
<!-- SPDX-License-Identifier: MIT-0 -->

# Local development harness

Run the full ARIA stack on your machine without a CDK deploy. The harness
emulates the AWS services that have local equivalents and shims the AgentCore
APIs that don't — **only Bedrock model calls go to real AWS**.

The local harness and AgentCore use the same LangGraph saver and DynamoDB
table schema. The harness configures a loopback DynamoDB endpoint.

## What runs where

| Deployed (AWS) | Local substitute |
|---|---|
| DynamoDB business decisions and audit history | DynamoDB Local, `p2p-dev-document-lifecycle` |
| LangGraph workflow checkpoints | DynamoDB Local, `p2p-dev-workflow-checkpoints` |
| S3 document bucket | Moto |
| Secrets Manager (ERPNext creds) | Moto |
| AgentCore Gateway (MCP tools → ERP) | `shims/mcp_gateway_shim.py` (FastMCP over the canonical API) |
| AgentCore Runtime (7 agents) | `shims/agentcore_shim.py` + `shims/run_agent.py` (`app.run(port=N)`) |
| AgentCore Memory (chat history) | `shims/agentcore_shim.py` (in-process event store) |
| Browser → AgentCore (SigV4) | `shims/agent_proxy_shim.py` (plain HTTP; no Cognito) |
| Cognito auth | guest mode (`window.ARIA_CONFIG.localMode`) |
| ERPNext (system of record) | `infra/docker/docker-compose.local-test.yaml` |
| **Bedrock (Claude)** | **real AWS** — the one real dependency |

The switch is botocore-native: the supervisor sets `AWS_ENDPOINT_URL_<SERVICE>`
for DynamoDB/S3/Secrets Manager/`bedrock-agentcore`, and leaves
`bedrock-runtime` unset so agent model calls hit real Bedrock. Application code
just calls `boto3.client(...)` with no endpoint argument.

## Prerequisites

- **AWS credentials with Bedrock access** and **Claude Sonnet model access
  enabled** in your region (see the main README "Bedrock model access" step).
  Everything else is emulated; Bedrock is real, so this is required.
- Python, Node/npm for the SPA, and Docker for ERPNext and DynamoDB Local.
  Run `python3 -m venv .venv` if needed, then install the Python dependencies
  with `.venv/bin/python -m pip install -r backend/requirements.txt 'moto[server]' 'fastmcp==2.14.7'`.
  Make also accepts an existing `~/.venv` or an explicit `PY` path.

## Quick start

```bash
cp local/env.example local/.env        # fill in AWS_ACCESS_KEY_ID / SECRET / (SESSION_TOKEN)

make -C local erpnext                   # 1) start local ERPNext (docker compose)
#   Open http://localhost:8080 and complete the Frappe setup wizard ONCE:
#   Company "Apex Manufacturing Group" (abbr AMG), USD/United States,
#   warehouse "Stores - AMG". (Same one-time step as a real deploy.)

(cd frontend && npm ci)
# Set ERPNext admin credentials in utilities/.env or in the shell.
# Install the unique workflow field on the existing site without reseeding it.
.venv/bin/python utilities/scripts/workflow_po_schema.py
make -C local up ARIA_SEED_ON_START=false P2P_LOCAL_APPROVER=1
make -C local ui                        # in a second terminal
```

Then open the Vite URL (http://localhost:5173). The SPA runs in guest mode and
routes agent invocations through the local proxy → agent → real Bedrock, with
ERP data from your local ERPNext.

**Run Full Workflow** on a test requisition calls the configured live Bedrock
model, which can incur AWS charges. Choose a requisition that needs human review
to exercise **Approve & Generate PO**. To retry an interrupted PO run, reopen
the workflow. If ERPNext could not confirm a prior write, the workflow shows a
review message and checks for the original order on reopen.

`make -C local up` starts DynamoDB Local and runs the supervisor in the foreground.
Ctrl-C stops the supervisor and its processes. DynamoDB Local, ERPNext, and the
SPA run separately. DynamoDB Local saves both workflow tables in the
`aria-p2p-dynamodb_dynamodb-data` Docker volume. Run
`make -C local dynamodb-stop` to stop the database and keep the volume.

When upgrading an existing local checkout, resolve any paused workflows on the
old setup first. The first DynamoDB Local startup provisions fresh tables.
The old Moto lifecycle records and SQLite checkpoints are separate from those
tables. Preserve any old audit history you need before stopping the old Moto
process. The ERPNext database stays in its existing volumes.

## Run the offline workflow tests

From the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt pytest 'moto[server]'
.venv/bin/python -m pytest -q backend/tests/test_workflow.py backend/tests/test_workflow_decisions.py backend/tests/test_erpnext_adapter.py::TestKeyedPurchaseOrders
```

The suite runs the AgentCore workflow entry point and decision API with the
DynamoDB saver, Moto, deterministic agent responses, and a test ERP adapter.
The process restart tests use a loopback Moto server and fresh Python workers.

To also verify database persistence with Docker running:

```bash
P2P_TEST_DYNAMODB_LOCAL=1 .venv/bin/python -m pytest -q backend/tests/test_dynamodb_local_restart.py
```

The Docker test uses isolated containers and a temporary Docker volume. It
replaces the database container after approval and again after a simulated ERP
commit followed by a worker crash. It checks that the original approval and
run survive, completed agent steps run once, and repeated resume returns one PO.

## Follow a workflow run

The supervisor prints JSON events for workflow steps, decisions, resume, and
outcomes. Search for the same `workflow_run_id` across the workflow agent and
decision API logs. To see a complete sample run without invoking Bedrock:

```bash
.venv/bin/python -m pytest -q -s backend/tests/test_workflow.py::test_human_approval_duplicate_click_and_resume
```

In AWS, the workflow runtime exports OpenTelemetry spans and emits
`ARIA/Workflow` metrics through CloudWatch Logs. Search by `workflow_run_id` to
join separate invocations. The dashboard separates human approval wait from
processing time and shows workflow outcomes, tool failures, and token usage.
The lifecycle table is the record of approval and PO outcome.
Subscribe the staging operator to the `p2p-dev-workflow-alerts` SNS topic
and verify both alarms after deploying the stack.

A `PO_REVIEW` event means the operator should open the matching requisition
in ARIA, inspect the confirmed order IDs, and check the order in ERPNext before
creating another PO. Reopen the workflow to run reconciliation.

## Replay the labeled requisition eval set

```bash
PYTHONPATH=backend .venv/bin/python -m evals
```

The runner uses the real LangGraph workflow, deterministic agent outputs, a
simulated ERP, and Moto. It reports the observed behavior against independently
labeled expectations and exits with code 1 when a case fails. Use
`--predictions results.json --model-id MODEL --prompt-version VERSION` to
replay previously collected structured sourcing and requisition outputs keyed
by case ID. The runner makes no model calls in either mode.

For interviews, show a run in the test logs followed by the eval report.
The eval set covers approvals, stale requests, split awards, and recovery after
a lost PO response. The same 16-case dataset previously caught three unsafe
automatic approvals. The workflow now checks the reviewed ERP total, line
amounts, currency, and cost-center budget before automatically approving a
purchase. All 16 fixture cases pass, including understated totals and missing
or conflicting budget evidence. The remaining-budget check is a point-in-time
read. Staging verification uses real ERPNext and Bedrock separately from the
fixture replay.

PO writes use the reviewed requisition prices and quantities. The ERP lookup
for an existing PO is read-only. Submitting a recovered draft requires a new
check of the approved requisition and the draft's commercial fields.
A submitted order with mismatched currency, items, quantities, or prices stays
in `PO_REVIEW` for an operator.

## Ports

| Port | Process |
|---|---|
| 5173 | SPA (Vite dev server) |
| 8000 | Backend (`uvicorn main:app`) |
| 8001 | Canonical API (in-process under the MCP gateway shim) |
| 8002 | MCP gateway shim (`/mcp`) |
| 8003 | Agent proxy shim (browser-facing) |
| 8080 | ERPNext (docker compose) |
| 8081–8087 | The seven agent processes |
| 9000 | AgentCore shim (runtime + memory) |
| 5100 | Moto (S3 and Secrets Manager) |
| 8100 | DynamoDB Local (checkpoints, lifecycle, and other DynamoDB tables) |

Override any of these in `local/.env` (see `local/config/harness_env.py`).

## How the pieces fit

- `config/harness_env.py` — single source of truth for ports and the
  `AWS_ENDPOINT_URL_*` / app env every process inherits.
- `supervisor.py` — starts Moto and launches the shims, seven agents, and backend.
  Ctrl-C stops those processes.
- `docker-compose.dynamodb.yaml` — starts DynamoDB Local with persistent storage.
- `shims/` — the AWS-API stand-ins (see the table above). Each is also runnable
  standalone for debugging.
- `scripts/init_aws.py` — provisions DynamoDB Local tables and the Moto bucket and
  secret. The workflow uses the same saver in AgentCore, where CDK sets
  `P2P_CHECKPOINT_TABLE` to the managed checkpoint table.
- `scripts/seed_local.py` — drives the canonical `utilities/scripts/04`+`05` seed
  scripts against local ERPNext (the scripts stay in `utilities/` — they're the
  shared runbook for real deploys too).

## Limitations

- **CodeInterpreter** (used by payment/sourcing/invoice agents for math) is an
  AgentCore resource with no local equivalent; those agents fall back gracefully
  (the code already guards it with try/except) and run without it locally.
- **Textract** invoice OCR has no local emulator, but extraction is Bedrock-first
  (real, works here); Textract is only a fallback if Bedrock fails.
- Guest mode bypasses Cognito, so per-user ERP identity / RBAC nuances differ
  from a real deploy.
