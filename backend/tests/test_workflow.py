# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Exercise the actual workflow entry point with durable state and deterministic agents."""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import boto3
import pytest
from fastapi.testclient import TestClient

from config import settings
from services import dynamo, lifecycle

DOCUMENT_ID = "MR-0001"
BACKEND = Path(__file__).resolve().parents[1]
CLAIMS = {"sub": "approver-1", "email": "approver@example.com", "p2p_role": "approver"}


class TestERP:
    """File-backed ERP test adapter. Successful writes survive a runtime exit."""

    __test__ = False

    def __init__(self, path: Path, scenario: str = "human"):
        self.path = path
        self.scenario = scenario
        self.recovery_creates = []
        self.draft_submissions = []
        self.allow_draft_submission = False
        self.outstanding_supplier_active = True
        self.request_overrides = {}
        self.budget_calls = []
        self.budget_rows = [{"cost_center": "Main - AMG", "currency": "USD", "remaining": 110}]
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS calls (agent TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS orders (operation_key TEXT PRIMARY KEY, payload TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS requisition (quantity INTEGER)")
            db.execute("INSERT INTO requisition SELECT 2 WHERE NOT EXISTS (SELECT 1 FROM requisition)")

    def set_request_qty(self, quantity: int):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE requisition SET quantity = ?", (quantity,))

    def calls(self) -> list[str]:
        with sqlite3.connect(self.path) as db:
            return [row[0] for row in db.execute("SELECT agent FROM calls ORDER BY rowid")]

    def order_ids(self) -> list[str]:
        with sqlite3.connect(self.path) as db:
            return [json.loads(row[0])["order_id"] for row in db.execute("SELECT payload FROM orders ORDER BY rowid")]

    def requisition(self):
        with sqlite3.connect(self.path) as db:
            quantity = db.execute("SELECT quantity FROM requisition").fetchone()[0]
        items = [{
            "item_id": "ITEM-1", "quantity": quantity, "unit_price": 55, "line_number": 1,
            "delivery_date": "2027-02-01", "warehouse": "Stores - AMG", "unit_of_measure": "Unit",
        }]
        if self.scenario.startswith("split"):
            items.append({
                "item_id": "ITEM-2", "quantity": 3, "unit_price": 25,
                "line_number": 2, "delivery_date": "2027-03-01",
            })
        return {
            "requisition_id": DOCUMENT_ID, "status": "submitted", "line_items": items,
            "total_amount": 110, "currency": "USD", "cost_center": "Main - AMG", **self.request_overrides,
        }

    async def call_tool(self, name, arguments):
        if name == "erp___get_requisition":
            return self.requisition()
        if name == "erp___get_budget_status":
            self.budget_calls.append(arguments)
            if self.budget_rows is None:
                raise TimeoutError("Trusted budget is unavailable")
            return {"budgets": self.budget_rows, "total_count": len(self.budget_rows)}
        if name == "erp___get_supplier":
            assert arguments["supplier_id"] == "SUP-2"
            return {"supplier_id": "SUP-2", "status": "active" if self.outstanding_supplier_active else "inactive"}
        if name == "erp___create_purchase_order":
            if arguments.get("recover_draft"):
                self.draft_submissions.append(arguments)
                if not self.allow_draft_submission:
                    raise TimeoutError("Draft is visible but submission did not finish")
                key = arguments["idempotency_key"]
                with sqlite3.connect(self.path) as db:
                    (saved,) = db.execute("SELECT payload FROM orders WHERE operation_key = ?", (key,)).fetchone()
                    order = json.loads(saved)
                    assert order["status"] == "draft"
                    order["status"] = "submitted"
                    db.execute("UPDATE orders SET payload = ? WHERE operation_key = ?", (json.dumps(order), key))
                return order
            self.recovery_creates.append(arguments)
            assert arguments["supplier_id"] == "SUP-2"
            assert len(arguments["line_items"]) == 1
            line = arguments["line_items"][0]
            assert (line["item_id"], line["quantity"], line["unit_price"], line["line_amount"]) == ("ITEM-2", 3, 25, 75)
            assert line["requisition_id"] == DOCUMENT_ID
            assert arguments["delivery_date"] == line["delivery_date"] == "2027-03-01"
            if self.scenario == "split_unknown":
                raise TimeoutError("No response and no recorded PO for the second allocation")
            order = {
                "order_id": "PO-0002", "total_amount": 75, "supplier_id": "SUP-2",
                "currency": arguments["currency"], "line_items": arguments["line_items"],
            }
            with sqlite3.connect(self.path) as db:
                db.execute("INSERT INTO orders VALUES (?, ?)", (arguments["idempotency_key"], json.dumps(order)))
            return order
        assert name == "erp___reconcile_purchase_order"
        with sqlite3.connect(self.path) as db:
            order = db.execute("SELECT payload FROM orders WHERE operation_key = ?", (
                arguments["idempotency_key"],
            )).fetchone()
        if not order:
            return {"status": "missing"}
        purchase_order = json.loads(order[0])
        return {"status": purchase_order.get("status", "submitted"), "purchase_order": purchase_order}

    async def run_agent(self, agent, prompt, emit, before_po=None):
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO calls VALUES (?)", (agent,))
        emit({"type": "progress", "agent": agent, "step": "Deterministic agent tool call"})
        if agent == "sourcing":
            result = {"recommended_vendor": {"supplier_id": "SUP-1", "supplier_name": "Supplier One", "score": 85}}
            if self.scenario.startswith("split"):
                result["split_award"] = [
                    {"supplier_id": "SUP-1", "items": ["ITEM-1"]},
                    {"supplier_id": "SUP-2", "items": ["ITEM-2"]},
                ]
            return result
        if agent == "requisition":
            assert "SUP-1" in prompt
            return {
                "recommendation": "REJECT" if self.scenario == "ai_reject" else "APPROVE",
                "risk_level": "MEDIUM" if self.scenario == "auto" else "HIGH",
                "total_amount": 110, "confidence": 0.95, "reasoning": "Test analysis",
            }
        assert agent == "po_management" and before_po
        if self.scenario.startswith("split"):
            assert "SUP-1" in prompt and "SUP-2" in prompt
        for index, item in enumerate(self.requisition()["line_items"], 1):
            proposed = {**item, "unit_price": 0.01, "delivery_date": "2031-01-01", "warehouse": "Other"}
            arguments = {
                "supplier_id": f"SUP-{index}", "line_items": [proposed], "payment_terms": "immediate",
                "currency": "EUR", "recover_draft": True,
                "user_email": "untrusted@example.com", "idempotency_key": "model-supplied",
            }
            assert before_po(arguments) is None
            assert arguments["idempotency_key"] != "model-supplied"
            assert arguments["currency"] == "USD" and arguments.get("recover_draft") is not True
            assert len(arguments["approved_request_version"]) == 64 and "payment_terms" not in arguments
            line = arguments["line_items"][0]
            assert line["requisition_id"] == DOCUMENT_ID
            for field in ("item_id", "quantity", "unit_price", "delivery_date", "warehouse", "unit_of_measure"):
                if field in item:
                    assert line[field] == item[field]
            if self.scenario == "unknown":
                raise TimeoutError("No response from ERP")
            if self.scenario != "auto":
                assert arguments["user_email"] == CLAIMS["email"]
            order = {
                "order_id": f"PO-{index:04}", "total_amount": 110, "supplier_id": arguments["supplier_id"],
                "currency": arguments["currency"], "line_items": arguments["line_items"],
            }
            if self.scenario == "draft":
                order["status"] = "draft"
            with sqlite3.connect(self.path) as db:
                db.execute("INSERT INTO orders VALUES (?, ?)", (
                    arguments["idempotency_key"], json.dumps(order),
                ))
            if self.scenario in {"lost", "draft"}:
                raise TimeoutError("Response lost after ERP committed")
            if self.scenario == "crash":
                os._exit(86)
            if self.scenario in {"split_partial", "split_unknown"}:
                break
        # The workflow gets authoritative IDs from reconciliation, not model text.
        return {"action": "CREATE", "created_order_id": "PO-NOT-REAL"}


@pytest.fixture
def lifecycle_table(mock_dynamo, monkeypatch):
    monkeypatch.setattr(dynamo, "dynamodb", mock_dynamo)
    checkpoint_table = f"{settings.dynamodb_table_prefix}-workflow-checkpoints"
    monkeypatch.setenv("P2P_CHECKPOINT_TABLE", checkpoint_table)
    create_checkpoint_table(mock_dynamo, checkpoint_table)
    return create_lifecycle_table(mock_dynamo)


@pytest.fixture
def env(tmp_path):
    return tmp_path / "test-erp.sqlite"


def create_checkpoint_table(resource, table_name):
    return resource.create_table(
        TableName=table_name,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def create_lifecycle_table(resource):
    return resource.create_table(
        TableName=f"{settings.dynamodb_table_prefix}-document-lifecycle",
        KeySchema=[{"AttributeName": "document_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "document_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


async def streamed_events(resume_from):
    import agentcore_app

    return [
        json.loads(line) async for line in agentcore_app.invoke({
            "document_id": DOCUMENT_ID, "resume_from": resume_from,
        })
    ]


async def invoke_runtime(erp, resume_from=""):
    import agentcore_app
    from workflow import strands_runtime

    with patch.object(agentcore_app, "AGENT_NAME", "workflow"), patch.object(
        strands_runtime, "StrandsWorkflowRuntime", return_value=erp,
    ):
        return await streamed_events(resume_from)


def invoke(erp, resume_from=""):
    return asyncio.run(invoke_runtime(erp, resume_from))


def approve(review, **overrides):
    from main import app

    async def verified_gateway(scope, receive, send):
        scope["aws.event"] = {"requestContext": {"authorizer": {"jwt": {"claims": CLAIMS}}}}
        await app(scope, receive, send)

    body = {
        "document_type": "PR", "document_id": DOCUMENT_ID, "action": "APPROVE",
        "workflow_run_id": review["workflow_run_id"], "request_version": review["request_version"],
        **overrides,
    }
    return TestClient(verified_gateway).post("/api/decisions/", json=body)


def payload(events):
    assert events[-1]["type"] == "result", events
    return events[-1]["result"]


def check_pending(events):
    result = payload(events)
    assert result["workflow_status"] == "PENDING_APPROVAL"
    assert result["status"] == "AWAITING_APPROVAL"
    assert result["workflow_run_id"] and result["request_version"]
    assert result["sourcing_result"]["recommended_vendor"]["supplier_id"] == "SUP-1"
    assert any(event.get("step", "").startswith("Step 2 complete") for event in events)
    return result


def check_complete(events, ids):
    result = payload(events)
    assert result["workflow_status"] == "COMPLETE"
    assert result["created_order_id"] == ids[0]
    assert result["created_order_ids"] == ids
    assert lifecycle.get_workflow(DOCUMENT_ID)["po_order_id"] == ids[0]


def test_automatic_approval(lifecycle_table, env):
    erp = TestERP(env, "auto")
    result = invoke(erp)
    check_complete(result, ["PO-0001"])
    assert payload(result)["auto_approved"]
    assert erp.budget_calls == [{"cost_center": "Main - AMG"}]
    assert erp.calls() == ["sourcing", "requisition", "po_management"]


@pytest.mark.parametrize("request_overrides", [
    {"total_amount": 5001}, {"total_amount": 200}, {"total_amount": None},
    {"total_amount": 0}, {"currency": "CAD"}, {"cost_center": None},
])
def test_automatic_approval_requires_consistent_erp_requisition(lifecycle_table, env, request_overrides):
    erp = TestERP(env, "auto")
    erp.request_overrides = request_overrides
    result = check_pending(invoke(erp))
    assert not result["auto_approved"]
    assert erp.order_ids() == []


def test_automatic_approval_rejects_understated_erp_header(lifecycle_table, env):
    erp = TestERP(env, "auto")
    erp.request_overrides = {
        "line_items": [{"item_id": "ITEM-1", "quantity": 2, "unit_price": 60}],
    }
    assert not check_pending(invoke(erp))["auto_approved"]
    assert erp.budget_calls == []
    assert erp.order_ids() == []


@pytest.mark.parametrize("budget_rows", [
    None,
    [],
    [{"cost_center": "Other Main - AMG", "currency": "USD", "remaining": 1000}],
    [{"cost_center": "Main - AMG", "currency": "CAD", "remaining": 1000}],
    [{"cost_center": "Main - AMG", "currency": "USD", "remaining": 109}],
    [{"cost_center": "Main - AMG", "currency": "USD", "remaining": 1000, "exceeded": True}],
    [{"cost_center": "Main - AMG", "currency": "USD", "remaining": "unknown"}],
    [{"cost_center": "Main - AMG", "currency": "USD", "remaining": 1000}] * 2,
])
def test_automatic_approval_requires_one_sufficient_erp_budget(lifecycle_table, env, budget_rows):
    erp = TestERP(env, "auto")
    erp.budget_rows = budget_rows
    result = check_pending(invoke(erp))
    assert not result["auto_approved"]
    assert erp.budget_calls == [{"cost_center": "Main - AMG"}]
    assert erp.order_ids() == []


def test_human_approval_duplicate_click_and_resume(lifecycle_table, env):
    erp = TestERP(env)
    assert invoke(erp, "po_generation")[-1]["type"] == "error"
    assert not erp.calls()
    review = check_pending(invoke(erp))
    assert invoke(erp, "po_generation")[-1]["error"] == "APPROVAL_REQUIRED"
    first = approve(review)
    assert first.status_code == 200, first.text
    assert approve(review).json() == first.json()
    check_complete(invoke(erp, "po_generation"), ["PO-0001"])
    check_complete(invoke(erp), ["PO-0001"])
    assert approve(review).json() == first.json()
    assert erp.order_ids() == ["PO-0001"]
    assert erp.calls() == ["sourcing", "requisition", "po_management"]


@pytest.mark.parametrize("scenario", ["human", "ai_reject"])
def test_rejection_does_not_create_a_po(lifecycle_table, env, scenario):
    erp = TestERP(env, scenario)
    events = invoke(erp)
    if scenario == "human":
        review = check_pending(events)
        assert approve(review, action="REJECT").status_code == 200
        assert approve(review).status_code == 409
        events = invoke(erp)
    assert payload(events)["workflow_status"] == "REJECTED"
    assert erp.calls() == ["sourcing", "requisition"]
    assert not erp.order_ids()


def test_concurrent_resumes_only_one_creates(lifecycle_table, env):
    import agentcore_app
    from workflow import strands_runtime

    erp = TestERP(env)
    review = check_pending(invoke(erp))
    assert approve(review).status_code == 200

    async def run_both():
        with patch.object(agentcore_app, "AGENT_NAME", "workflow"), patch.object(
            strands_runtime, "StrandsWorkflowRuntime", return_value=erp,
        ):
            return await asyncio.gather(streamed_events("po_generation"), streamed_events("po_generation"))

    results = [payload(events)["workflow_status"] for events in asyncio.run(run_both())]
    assert "COMPLETE" in results
    assert all(status in {"COMPLETE", "RUNNING"} for status in results)
    assert erp.calls() == ["sourcing", "requisition", "po_management"]
    assert erp.order_ids() == ["PO-0001"]


def test_stale_submission_and_changed_erp_requisition(lifecycle_table, env):
    erp = TestERP(env)
    review = check_pending(invoke(erp))
    assert approve(review, request_version="old-version").status_code == 409
    assert approve(review, workflow_run_id="older-run").status_code == 409
    assert approve(review).status_code == 200
    erp.set_request_qty(4)
    result = payload(invoke(erp))
    assert result["workflow_status"] == "FAILED"
    assert result["error_code"] == "STALE_APPROVAL"
    assert erp.calls() == ["sourcing", "requisition"]
    assert not erp.order_ids()


def test_lost_po_response_recovers_erp_id(lifecycle_table, env):
    erp = TestERP(env, "lost")
    review = check_pending(invoke(erp))
    assert approve(review).status_code == 200
    check_complete(invoke(erp, "po_generation"), ["PO-0001"])
    assert erp.order_ids() == ["PO-0001"]


@pytest.mark.parametrize("change_request", [False, True])
def test_draft_submission_rechecks_approved_request(lifecycle_table, env, change_request):
    erp = TestERP(env, "draft")
    review = check_pending(invoke(erp))
    assert approve(review).status_code == 200
    assert payload(invoke(erp, "po_generation"))["status"] == "PO_REVIEW"
    [first_attempt] = erp.draft_submissions
    assert first_attempt["approved_request_version"] == review["request_version"]
    assert erp.order_ids() == ["PO-0001"]

    erp.allow_draft_submission = True
    if change_request:
        erp.set_request_qty(3)
        result = payload(invoke(erp, "po_generation"))
        assert result["status"] == "PO_REVIEW"
        assert result["error_code"] == "STALE_APPROVAL"
        assert erp.draft_submissions == [first_attempt]
    else:
        check_complete(invoke(erp, "po_generation"), ["PO-0001"])
        assert [attempt["idempotency_key"] for attempt in erp.draft_submissions] == [
            first_attempt["idempotency_key"], first_attempt["idempotency_key"],
        ]
    assert erp.order_ids() == ["PO-0001"]
    assert erp.calls() == ["sourcing", "requisition", "po_management"]


@pytest.mark.parametrize("recovered_rate", [55, 56])
def test_unknown_po_outcome_blocks_another_create(lifecycle_table, env, recovered_rate):
    erp = TestERP(env, "unknown")
    review = check_pending(invoke(erp))
    assert approve(review).status_code == 200
    for _ in range(2):
        result = payload(invoke(erp))
        assert result["status"] == "PO_REVIEW"
        assert result["error_code"] == "PO_OUTCOME_UNKNOWN"
        assert lifecycle.get_workflow(DOCUMENT_ID)["status"] == "PO_REVIEW"
    assert erp.calls() == ["sourcing", "requisition", "po_management"]
    assert not erp.order_ids()
    (operation_key,) = lifecycle.get_workflow(DOCUMENT_ID)["run"]["po_attempts"]
    recovered = {
        "order_id": "PO-0001", "supplier_id": "SUP-1", "currency": "USD", "total_amount": recovered_rate * 2,
        "line_items": [{
            **erp.requisition()["line_items"][0], "requisition_id": DOCUMENT_ID, "unit_price": recovered_rate,
        }],
    }
    with sqlite3.connect(erp.path) as db:
        db.execute("INSERT INTO orders VALUES (?, ?)", (operation_key, json.dumps(recovered)))
    events = invoke(erp)
    if recovered_rate == 55:
        check_complete(events, ["PO-0001"])
    else:
        assert payload(events)["status"] == "PO_REVIEW"
        assert erp.order_ids() == ["PO-0001"]
    assert erp.calls() == ["sourcing", "requisition", "po_management"]


def test_review_reopens_if_audit_projection_was_interrupted(lifecycle_table, env):
    erp = TestERP(env)
    with patch.object(lifecycle, "record_workflow_gate", side_effect=RuntimeError("temporary write failure")):
        assert invoke(erp)[-1]["error"] == "WORKFLOW_ERROR"
    assert lifecycle.get_workflow(DOCUMENT_ID)["status"] == "ANALYZING"
    pending = payload(invoke(erp))
    assert pending["status"] == "AWAITING_APPROVAL"
    assert approve(pending).status_code == 200
    check_complete(invoke(erp), ["PO-0001"])
    assert erp.calls() == ["sourcing", "requisition", "po_management"]


def test_legacy_pending_keeps_run_and_gets_versioned_review(lifecycle_table, env):
    run_id = lifecycle.add_run_entry(DOCUMENT_ID, "workflow", agent="workflow", status="pending_approval")
    lifecycle.add_run_entry(
        DOCUMENT_ID, "decision", parent_id=run_id, action="AI_ESCALATED", status="pending_approval",
    )
    lifecycle.set_status(DOCUMENT_ID, lifecycle.Status.PENDING_APPROVAL)
    erp = TestERP(env, "auto")
    review = check_pending(invoke(erp))
    assert review["workflow_run_id"] == run_id
    assert not review["auto_approved"]
    assert approve(review).status_code == 200
    check_complete(invoke(erp), ["PO-0001"])


def test_split_award_recovery_retains_supplier_allocations(lifecycle_table, env):
    erp = TestERP(env, "split")
    review = check_pending(invoke(erp))
    assert approve(review).status_code == 200
    check_complete(invoke(erp, "po_generation"), ["PO-0001", "PO-0002"])
    check_complete(invoke(erp), ["PO-0001", "PO-0002"])
    assert erp.order_ids() == ["PO-0001", "PO-0002"]


def test_reopens_partial_split_and_creates_only_unsent_allocation(lifecycle_table, env):
    erp = TestERP(env, "split_partial")
    erp.outstanding_supplier_active = False
    review = check_pending(invoke(erp))
    assert approve(review).status_code == 200
    partial = payload(invoke(erp, "po_generation"))
    assert partial["workflow_status"] == "PO_REVIEW"
    assert partial["error_code"] == "SUPPLIER_UNAVAILABLE"
    assert partial["created_order_ids"] == ["PO-0001"]
    assert len(lifecycle.get_workflow(DOCUMENT_ID)["run"]["po_attempts"]) == 1

    erp.outstanding_supplier_active = True
    check_complete(invoke(erp), ["PO-0001", "PO-0002"])
    check_complete(invoke(erp), ["PO-0001", "PO-0002"])
    assert [order["supplier_id"] for order in erp.recovery_creates] == ["SUP-2"]
    assert erp.order_ids() == ["PO-0001", "PO-0002"]
    assert erp.calls() == ["sourcing", "requisition", "po_management"]


def test_preserves_first_receipt_and_does_not_resend_unknown_second_allocation(lifecycle_table, env):
    erp = TestERP(env, "split_unknown")
    review = check_pending(invoke(erp))
    assert approve(review).status_code == 200
    for _ in range(2):
        partial = payload(invoke(erp))
        assert partial["workflow_status"] == "PO_REVIEW"
        assert partial["error_code"] == "PO_OUTCOME_UNKNOWN"
        assert partial["created_order_ids"] == ["PO-0001"]
    assert len(erp.recovery_creates) == 1
    assert erp.order_ids() == ["PO-0001"]


@pytest.mark.parametrize("structured", [True, False])
def test_wrapped_fastmcp_order_confirmation_unblocks_next_allocation(structured):
    from workflow.strands_runtime import CREATE_PO, _WorkflowToolGuard

    guard = _WorkflowToolGuard(lambda _: None)
    first = SimpleNamespace(tool_use={"name": CREATE_PO, "toolUseId": "first", "input": {}}, cancel_tool=False)
    guard.before_tool(first)
    response = {"result": {"order_id": "PO-0001"}}
    first.result = {
        "status": "success",
        **({"structuredContent": response} if structured else {"content": [{"text": json.dumps(response)}]}),
    }
    guard.after_tool(first)
    second = SimpleNamespace(tool_use={"name": CREATE_PO, "toolUseId": "second", "input": {}}, cancel_tool=False)
    guard.before_tool(second)
    assert not second.cancel_tool
    assert guard.unconfirmed_call == "second"


@pytest.mark.parametrize("crash_after_commit", [False, True])
def test_fresh_process_resumes_saved_approval(env, monkeypatch, crash_after_commit):
    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    try:
        host, port = server.get_host_and_port()
        endpoint = f"http://{host}:{port}"
        prefix = f"p2p-restart-{uuid4().hex}"
        checkpoint_table = f"{prefix}-workflow-checkpoints"
        monkeypatch.setenv("AWS_ENDPOINT_URL_DYNAMODB", endpoint)
        monkeypatch.setenv("DYNAMODB_TABLE_PREFIX", prefix)
        monkeypatch.setenv("P2P_CHECKPOINT_TABLE", checkpoint_table)
        monkeypatch.setattr(settings, "dynamodb_table_prefix", prefix)
        monkeypatch.setenv("PYTHONPATH", str(BACKEND))
        resource = boto3.resource("dynamodb", region_name=settings.aws_region, endpoint_url=endpoint)
        monkeypatch.setattr(dynamo, "dynamodb", resource)
        table = create_lifecycle_table(resource)
        create_checkpoint_table(resource, checkpoint_table)

        def worker(resume_from="", scenario="human", expected_exit=0):
            completed = subprocess.run(
                [sys.executable, __file__, str(env), resume_from, scenario],
                cwd=BACKEND, capture_output=True, text=True, timeout=25,
            )
            assert completed.returncode == expected_exit, completed.stderr
            return [] if expected_exit else json.loads(completed.stdout)

        review = check_pending(worker())
        erp = TestERP(env)
        assert erp.calls() == ["sourcing", "requisition"]
        assert approve(review).status_code == 200
        if crash_after_commit:
            worker("po_generation", "crash", expected_exit=86)
            assert lifecycle.get_workflow(DOCUMENT_ID)["status"] == "PO_GENERATION"
            assert erp.order_ids() == ["PO-0001"]
            table.update_item(
                Key={"document_id": DOCUMENT_ID}, UpdateExpression="SET workflow_lease_until = :expired",
                ExpressionAttributeValues={":expired": 1},
            )
        check_complete(worker("po_generation"), ["PO-0001"])
        assert erp.calls() == ["sourcing", "requisition", "po_management"]
        assert erp.order_ids() == ["PO-0001"]
    finally:
        server.stop()


if __name__ == "__main__":
    print(json.dumps(invoke(TestERP(Path(sys.argv[1]), sys.argv[3]), sys.argv[2])))
