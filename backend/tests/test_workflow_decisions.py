# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.decisions import router
from services import dynamo, lifecycle


@pytest.fixture
def lifecycle_table(mock_dynamo, monkeypatch):
    from config import settings

    monkeypatch.setattr(dynamo, "dynamodb", mock_dynamo)
    return mock_dynamo.create_table(
        TableName=f"{settings.dynamodb_table_prefix}-document-lifecycle",
        KeySchema=[{"AttributeName": "document_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "document_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


def decision_client(claims=None):
    app = FastAPI()
    app.include_router(router)

    async def gateway(scope, receive, send):
        if claims is not None:
            scope["aws.event"] = {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}
        await app(scope, receive, send)

    return TestClient(gateway, client=("127.0.0.1", 8000))


def pending_decision(document_id="MR-test"):
    run = lifecycle.claim_workflow(document_id, "worker-one")
    lifecycle.record_workflow_gate(
        document_id, run["run_id"], "version-one", "AI_ESCALATED", lease_owner="worker-one",
    )
    return {
        "document_type": "PR", "document_id": document_id, "action": "APPROVE",
        "workflow_run_id": run["run_id"], "request_version": "version-one",
    }


def test_workflow_decision_requires_verified_approver(lifecycle_table):
    body = pending_decision()
    response = decision_client().post("/", json=body, headers={"x-amz-user": "admin"})
    assert response.status_code == 401
    response = decision_client({"sub": "requester", "p2p_role": "requester", "custom:role": "admin"}).post(
        "/", json=body,
    )
    assert response.status_code == 403
    assert lifecycle.get_workflow(body["document_id"])["status"] == "PENDING_APPROVAL"


def test_workflow_decision_rejects_stale_or_missing_review(lifecycle_table):
    body = pending_decision()
    client = decision_client({"sub": "approver", "p2p_role": "approver"})
    for overrides in ({"workflow_run_id": "old-workflow"}, {"request_version": "old-version"},
                      {"workflow_run_id": "", "request_version": ""}, {"document_type": "INVOICE"}):
        assert client.post("/", json={**body, **overrides}).status_code == 409
    assert lifecycle.get_workflow(body["document_id"])["status"] == "PENDING_APPROVAL"


def test_repeated_approval_preserves_completed_purchase_order(lifecycle_table):
    body = pending_decision()
    document_id, run_id = body["document_id"], body["workflow_run_id"]
    client = decision_client({"sub": "approver", "email": "approval@example.com", "p2p_role": "approver"})
    first = client.post("/", json=body)
    assert first.status_code == 200
    assert first.json()["decided_by"] == "approval@example.com"
    assert lifecycle.get_workflow(document_id)["decision"]["approver_email"] == "approval@example.com"
    assert lifecycle.record_po_attempt(document_id, run_id, "order-key", "worker-one")
    assert not lifecycle.record_po_attempt(document_id, run_id, "order-key", "worker-one")
    lifecycle.record_workflow_po(document_id, run_id, "PO-0001", ["PO-0001"], 50, "CREATE", {}, lease_owner="worker-one")

    assert client.post("/", json=body).json() == first.json()
    assert client.post("/", json={**body, "action": "REJECT"}).status_code == 409
    context = lifecycle.get_workflow(document_id)
    assert context["status"] == "PO_CREATED"
    assert context["po_order_id"] == "PO-0001"
    assert len([run for run in context["runs"] if run["type"] == "decision"]) == 1


def test_local_approver_requires_explicit_loopback_opt_in(lifecycle_table, monkeypatch):
    body = pending_decision()
    client = decision_client()
    monkeypatch.setenv("DEPLOYMENT_ENV", "local")
    monkeypatch.delenv("P2P_LOCAL_APPROVER", raising=False)
    assert client.post("/", json=body).status_code == 401
    monkeypatch.setenv("P2P_LOCAL_APPROVER", "1")
    assert client.post("/", json=body).json()["decided_by"] == "local-approver"


def test_standalone_pr_and_invoice_decisions_keep_existing_route(lifecycle_table):
    client = decision_client()
    for document_type, status in (("PR", "APPROVED"), ("INVOICE", "REJECTED")):
        response = client.post("/", json={
            "document_type": document_type, "document_id": f"DOC-{document_type}",
            "action": "APPROVE" if status == "APPROVED" else "REJECT", "justification": "Standalone decision",
        })
        assert response.status_code == 200
        assert lifecycle.get_lifecycle(f"DOC-{document_type}")["status"] == status


def test_rechecking_legacy_approval_preserves_previous_audit(lifecycle_table):
    document_id = "MR-legacy"
    run_id = lifecycle.add_run_entry(document_id, "workflow", agent="workflow", status="approved")
    lifecycle.add_run_entry(
        document_id, "decision", parent_id=run_id, action="HUMAN_APPROVED",
        decided_by="prior-approver", status="approved",
    )
    lifecycle.set_status(document_id, lifecycle.Status.APPROVED)
    reopened = lifecycle.claim_workflow(document_id, "worker-one")
    assert reopened["run_id"] == run_id
    assert reopened["request_version"] == ""

    for _ in range(2):
        review = lifecycle.record_workflow_gate(
            document_id, run_id, "reviewed-version", "AI_ESCALATED", lease_owner="worker-one",
        )
    assert review["status"] == "PENDING_APPROVAL"
    assert review["decision"]["request_version"] == "reviewed-version"
    assert [run["action"] for run in review["runs"] if run["type"] == "decision"] == [
        "HUMAN_APPROVED", "AI_ESCALATED",
    ]


def test_workflow_lease_has_one_owner_and_expired_owner_cannot_write(lifecycle_table):
    def claim(owner):
        try:
            return lifecycle.claim_workflow("MR-test", owner)
        except lifecycle.WorkflowConflict as error:
            assert error.code == "DUPLICATE_WORKFLOW"
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(claim, ("worker-one", "worker-two")))
    (winner,) = [claimed for claimed in claims if claimed]
    run_id, owner = winner["run_id"], winner["lease_owner"]
    assert lifecycle.renew_workflow_lease("MR-test", run_id, owner)
    lifecycle_table.update_item(
        Key={"document_id": "MR-test"}, UpdateExpression="SET workflow_lease_until = :expired",
        ExpressionAttributeValues={":expired": 1},
    )

    restarted = lifecycle.claim_workflow("MR-test", "replacement")
    assert restarted["run_id"] == run_id
    assert not lifecycle.renew_workflow_lease("MR-test", run_id, owner)
    assert not lifecycle.release_workflow_lease("MR-test", run_id, owner)
    with pytest.raises(lifecycle.WorkflowConflict, match="lost its lease"):
        lifecycle.record_workflow_status("MR-test", run_id, "FAILED", lease_owner=owner)
