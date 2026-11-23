# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Tests for main.py FastAPI routes.

Tests routes mounted on the operational API and the legacy error router in isolation.

Uses the app_client fixture from conftest.py (moto-backed DynamoDB).
"""

import uuid
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

APPROVER_EMAIL = "sarah.johnson@example.test"


@pytest.fixture
def authenticated_app_client(app_client):
    """Supply the verified Cognito claims passed by API Gateway to the app."""
    async def gateway(scope, receive, send):
        scope["aws.event"] = {
            "requestContext": {"authorizer": {"jwt": {"claims": {"email": APPROVER_EMAIL}}}},
        }
        await app_client.app(scope, receive, send)

    with TestClient(gateway) as client:
        yield client


@pytest.fixture
def legacy_errors_client(mock_dynamo, monkeypatch):
    """Exercise the error handlers without exposing unprovisioned routes in main.py."""
    from api.errors import router
    from services import dynamo

    monkeypatch.setattr(dynamo, "dynamodb", mock_dynamo)
    app = FastAPI()
    app.include_router(router, prefix="/api/errors")
    with TestClient(app) as client:
        yield client


# ---------------------------------------------------------------------------
# Health Check
# ---------------------------------------------------------------------------

class TestHealthCheck:
    def test_health_ok(self, app_client):
        resp = app_client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# 1. Agents routes (/api/agents/*)
# ---------------------------------------------------------------------------

class TestAgentRoutes:
    """Tests for chat and rate limits on the operational API."""

    def test_rate_limit_status(self, app_client):
        resp = app_client.get("/api/agents/rate-limit")
        assert resp.status_code == 200
        body = resp.json()
        assert "used" in body
        assert "limit" in body
        assert "remaining" in body
        assert "window_seconds" in body

    @patch("agents.chat_agent.invoke")
    def test_agent_chat(self, mock_chat_invoke, app_client):
        mock_chat_invoke.return_value = {"reply": "Hello from chat agent"}
        resp = app_client.post(
            "/api/agents/chat",
            json={"message": "What PRs need approval?", "role": "admin"},
        )
        assert resp.status_code == 200
        mock_chat_invoke.assert_called_once()

    @pytest.mark.parametrize("agent_name", ["unknown_agent", "requisition"])
    def test_specialized_agents_are_not_streamed_by_operational_api(self, app_client, agent_name):
        """Specialized agents are called directly through their runtimes."""
        resp = app_client.post(
            f"/api/agents/{agent_name}/stream",
            params={"document_id": "PR-001"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 2. Decisions routes (/api/decisions/*)
# ---------------------------------------------------------------------------

class TestDecisionRoutes:
    """Tests for api/decisions.py -- audit trail listing and recording."""

    def test_list_decisions_empty(self, app_client):
        resp = app_client.get("/api/decisions/")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)
        assert len(resp.json()) == 0

    def test_list_decisions_after_recording(self, authenticated_app_client):
        """Record a decision through POST /api/decisions/, then verify it appears."""
        approved = authenticated_app_client.post(
            "/api/decisions/",
            json={
                "document_type": "PR",
                "document_id": "MAT-REQ-AUDIT-001",
                "action": "APPROVE",
                "justification": "Test approval",
            },
        )
        assert approved.status_code == 200
        resp = authenticated_app_client.get("/api/decisions/")
        assert resp.status_code == 200
        decisions = resp.json()
        [decision] = decisions
        assert decision["document_id"] == "MAT-REQ-AUDIT-001"
        assert decision["action"] == "HUMAN_APPROVED"
        assert decision["decided_by"] == APPROVER_EMAIL
        assert decision["justification"] == "Test approval"


class TestDecisionRecording:
    """Tests for POST /api/decisions/ -- human approval/rejection."""

    def test_record_decision_approve(self, authenticated_app_client):
        resp = authenticated_app_client.post(
            "/api/decisions/",
            json={
                "document_type": "PR",
                "document_id": "MAT-REQ-00001",
                "action": "APPROVE",
                "justification": "Looks good",
                "agent_recommendation": "ESCALATE",
                "agent_confidence": 0.75,
                "agent_reasoning": "Over threshold",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["action"] == "APPROVE"
        assert body["document_id"] == "MAT-REQ-00001"
        assert body["decided_by"] == APPROVER_EMAIL
        assert body["decided_at"]
        audit = authenticated_app_client.get("/api/decisions/MAT-REQ-00001/runs")
        assert audit.status_code == 200
        [decision] = audit.json()["runs"]
        assert decision["id"]
        assert decision["action"] == "HUMAN_APPROVED"
        assert decision["justification"] == "Looks good"
        assert decision["confidence"] == 0.75

    def test_record_decision_reject(self, authenticated_app_client):
        resp = authenticated_app_client.post(
            "/api/decisions/",
            json={
                "document_type": "INVOICE",
                "document_id": "ACC-PINV-2025-00001",
                "action": "REJECT",
                "justification": "Price mismatch too large",
                "match_result": "DISCREPANCY",
                "agent_confidence": 0.6,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["action"] == "REJECT"
        assert body["document_id"] == "ACC-PINV-2025-00001"
        assert body["decided_by"] == APPROVER_EMAIL
        audit = authenticated_app_client.get("/api/decisions/ACC-PINV-2025-00001/runs")
        assert audit.status_code == 200
        [decision] = audit.json()["runs"]
        assert decision["action"] == "HUMAN_REJECTED"
        assert decision["justification"] == "Price mismatch too large"

    def test_record_decision_appears_in_list(self, authenticated_app_client):
        approved = authenticated_app_client.post(
            "/api/decisions/",
            json={
                "document_type": "PR",
                "document_id": "MAT-REQ-AUDIT-TEST",
                "action": "APPROVE",
            },
        )
        assert approved.status_code == 200
        resp = authenticated_app_client.get("/api/decisions/")
        assert resp.status_code == 200
        items = resp.json()
        found = [d for d in items if d.get("document_id") == "MAT-REQ-AUDIT-TEST"]
        assert len(found) >= 1
        assert found[0]["action"] == "HUMAN_APPROVED"


# ---------------------------------------------------------------------------
# 3. Errors routes (/api/errors/*)
# ---------------------------------------------------------------------------

class TestErrorRoutes:
    """Tests for the isolated legacy error router."""

    def _seed_error(self, error_id=None, retry_eligible=False, resolved=False):
        from services.dynamo import put_item
        eid = error_id or str(uuid.uuid4())
        put_item("agent-errors", {
            "error_id": eid,
            "agent_name": "requisition",
            "document_id": "PR-001",
            "error_type": "AnalysisError",
            "message": "Mock error",
            "severity": "HIGH",
            "category": "AGENT_FAILURE",
            "timestamp": "2026-04-09T10:00:00",
            "resolved": resolved,
            "retry_eligible": retry_eligible,
            "retries_attempted": 0,
            "max_retries": 3,
            "human_action_required": True,
        })
        return eid

    def test_list_errors_empty(self, legacy_errors_client):
        resp = legacy_errors_client.get("/api/errors/")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_list_errors_filters_resolved(self, legacy_errors_client):
        eid = self._seed_error(resolved=True)
        resp = legacy_errors_client.get("/api/errors/")
        ids = [e["error_id"] for e in resp.json()]
        assert eid not in ids

        resp = legacy_errors_client.get("/api/errors/", params={"resolved": "true"})
        ids = [e["error_id"] for e in resp.json()]
        assert eid in ids

    def test_get_error_by_id(self, legacy_errors_client):
        eid = self._seed_error()
        resp = legacy_errors_client.get(f"/api/errors/{eid}")
        assert resp.status_code == 200
        assert resp.json()["error_id"] == eid

    def test_get_error_not_found(self, legacy_errors_client):
        resp = legacy_errors_client.get("/api/errors/nonexistent-id")
        assert resp.status_code == 404

    def test_resolve_error(self, legacy_errors_client):
        eid = self._seed_error()
        resp = legacy_errors_client.post(f"/api/errors/{eid}/resolve", params={"resolved_by": "admin"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["resolved"] is True
        assert body["resolved_by"] == "admin"

    def test_retry_error_success(self, legacy_errors_client):
        eid = self._seed_error(retry_eligible=True)
        resp = legacy_errors_client.post(f"/api/errors/{eid}/retry")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "retry_queued"

    def test_retry_error_not_eligible(self, legacy_errors_client):
        eid = self._seed_error(retry_eligible=False)
        resp = legacy_errors_client.post(f"/api/errors/{eid}/retry")
        assert resp.status_code == 400

    def test_retry_error_not_found(self, legacy_errors_client):
        resp = legacy_errors_client.post("/api/errors/nonexistent-id/retry")
        assert resp.status_code == 404

    def test_error_summary_counts(self, legacy_errors_client):
        self._seed_error()
        resp = legacy_errors_client.get("/api/errors/summary/counts")
        assert resp.status_code == 200
        body = resp.json()
        assert "total_unresolved" in body
        assert "human_action_needed" in body
        assert body["total_unresolved"] >= 1


class TestLifecycleRoutes:
    @pytest.mark.parametrize("status", ["FAILED", "PO_REVIEW"])
    def test_list_filters_current_issues(self, app_client, status):
        from services.dynamo import put_item

        empty = app_client.get("/api/lifecycle/", params={"status": status})
        assert empty.status_code == 200
        assert empty.json() == {"lifecycles": []}

        for state in ["FAILED", "PO_REVIEW", "PO_CREATED"]:
            put_item("document-lifecycle", {"document_id": f"PR-{state}", "status": state})

        resp = app_client.get("/api/lifecycle/", params={"status": status})
        assert resp.status_code == 200
        assert resp.json() == {"lifecycles": [{"document_id": f"PR-{status}", "status": status}]}

    @patch("services.lifecycle.scan_table", side_effect=RuntimeError("Detailed internal storage error"))
    def test_list_returns_unavailable_when_store_fails(self, mock_scan, app_client):
        resp = app_client.get("/api/lifecycle/", params={"status": "PO_REVIEW"})
        assert resp.status_code == 503
        assert resp.json() == {"detail": "Lifecycle records are temporarily unavailable"}
        mock_scan.assert_called_once_with("document-lifecycle")


# ---------------------------------------------------------------------------
# 4. Config View routes (/api/config/*)
# ---------------------------------------------------------------------------

class TestConfigViewRoutes:
    """Tests for api/config_view.py -- read-only config endpoints."""

    def test_get_approval_rules(self, app_client):
        resp = app_client.get("/api/config/rules")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, dict)
        assert "hard_limits" in body
        assert "agent_rules" in body

    def test_get_agent_configs(self, app_client):
        resp = app_client.get("/api/config/agents")
        assert resp.status_code == 200
        agents = resp.json()
        assert isinstance(agents, list)
        assert len(agents) == 6
        agent_ids = [a["id"] for a in agents]
        assert "requisition" in agent_ids
        assert "sourcing" in agent_ids
        assert "payment" in agent_ids
        for agent in agents:
            assert "system_prompt" in agent


# ---------------------------------------------------------------------------
# 5. Dashboard routes (/api/dashboard/*)
# ---------------------------------------------------------------------------

class TestDashboardRoutes:
    """Tests for api/dashboard.py -- aggregated metrics."""

    def test_dashboard_metrics(self, app_client):
        resp = app_client.get("/api/dashboard/metrics")
        assert resp.status_code == 200
        body = resp.json()
        assert "total_spend" in body
        assert "total_orders" in body
        assert "currency" in body


# ---------------------------------------------------------------------------
# 6. Admin routes (/api/admin/*)
# ---------------------------------------------------------------------------

class TestAdminRoutes:
    """Tests for api/admin.py -- reset and status."""

    def test_admin_reset(self, app_client):
        resp = app_client.post("/api/admin/reset")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "reset_complete"
        assert "counts" in body

    def test_admin_reset_clears_decisions(self, authenticated_app_client):
        """Verify reset actually clears DynamoDB tables."""
        approval = authenticated_app_client.post(
            "/api/decisions/",
            json={
                "document_type": "PR",
                "document_id": "MAT-REQ-RESET-TEST",
                "action": "APPROVE",
            },
        )
        assert approval.status_code == 200
        resp = authenticated_app_client.get("/api/decisions/")
        assert len(resp.json()) >= 1

        reset = authenticated_app_client.post("/api/admin/reset")
        assert reset.status_code == 200
        assert reset.json()["counts"]["document-lifecycle"] == "1 deleted"

        resp = authenticated_app_client.get("/api/decisions/")
        assert len(resp.json()) == 0

    def test_admin_status_no_adapter(self, app_client):
        """With no ERP transport configured, status returns error."""
        resp = app_client.get("/api/admin/status")
        assert resp.status_code == 200
        body = resp.json()
        assert "error" in body
