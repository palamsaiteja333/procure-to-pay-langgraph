# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Decisions API — audit trail for agent runs, recommendations, and human decisions.

All entries are stored in the document-lifecycle table's `runs` array.
Supports tree structure: workflows contain nested step entries via parent_id.
"""

import json
import logging
import os
from ipaddress import ip_address
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from services import erp_client
from services.auth import _jwt_claims, get_authenticated_user, get_user_email
from services.lifecycle import (
    WorkflowConflict, get_lifecycle, get_workflow, list_lifecycles,
    record_standalone_decision, record_workflow_decision,
)
from utils import workflow_telemetry as telemetry

logger = logging.getLogger("p2p.api.decisions")

router = APIRouter()


class DecisionRequest(BaseModel):
    document_type: str          # "PR" or "INVOICE"
    document_id: str
    action: Literal["APPROVE", "REJECT"]
    justification: str = ""
    agent_recommendation: str = ""
    agent_confidence: float = 0.0
    agent_reasoning: str = ""
    match_result: str = ""
    workflow_run_id: str = ""
    request_version: str = ""


@router.get("/")
def list_decisions():
    """List all run entries across all documents, most recent first.

    Returns top-level entries (workflows, standalone analyses, decisions)
    with their children available via the per-document endpoint.
    """
    lifecycles = list_lifecycles()
    all_entries = []

    for lc in lifecycles:
        doc_id = lc.get("document_id", "")
        doc_type = lc.get("document_type", "PR")

        runs = lc.get("runs", [])
        if isinstance(runs, str):
            try:
                runs = json.loads(runs)
            except (json.JSONDecodeError, TypeError):
                runs = []

        for r in runs:
            entry_type = r.get("type", "")
            parent_id = r.get("parent_id")

            # Include: workflow entries, ALL decision entries, and receiving agent standalone
            # invoice_matching and payment standalone analyses are read-only previews — excluded
            # They only appear as children of a payment_workflow
            is_downstream_analysis = (
                entry_type == "analysis"
                and not parent_id
                and r.get("agent", "") in ("receiving",)
            )
            if entry_type == "workflow" or entry_type == "decision" or is_downstream_analysis:
                all_entries.append({
                    "decision_id": r.get("id", ""),
                    "document_type": doc_type,
                    "document_id": doc_id,
                    "type": entry_type,
                    "agent": r.get("agent", ""),
                    "action": r.get("action") or r.get("recommendation") or "",
                    "status": r.get("status", ""),
                    "decided_by": r.get("decided_by") or ("AI_AGENT" if entry_type != "decision" else ""),
                    "decided_at": r.get("completed_at") or r.get("started_at") or "",
                    "recommendation": r.get("recommendation", ""),
                    "confidence": r.get("confidence", 0),
                    "summary": r.get("summary", ""),
                    "justification": r.get("justification", ""),
                    "parent_id": parent_id or "",
                    # For backward compat with existing frontend
                    "agent_recommendation": r.get("recommendation") or r.get("action") or "",
                    "agent_confidence": str(r.get("confidence", 0) or 0),
                    "agent_reasoning": r.get("summary", ""),
                })

        # Also include legacy decisions[] entries for backward compat
        decisions = lc.get("decisions", [])
        if isinstance(decisions, str):
            try:
                decisions = json.loads(decisions)
            except (json.JSONDecodeError, TypeError):
                decisions = []

        # Only include legacy decisions if no runs[] exist (migration period)
        if not runs and decisions:
            for d in decisions:
                all_entries.append({
                    "decision_id": d.get("id", ""),
                    "document_type": doc_type,
                    "document_id": doc_id,
                    "type": "decision",
                    "agent": "",
                    "action": d.get("action", ""),
                    "status": "completed",
                    "decided_by": d.get("decided_by", ""),
                    "decided_at": d.get("decided_at", ""),
                    "recommendation": d.get("action", ""),
                    "confidence": d.get("confidence", 0),
                    "summary": "",
                    "justification": d.get("justification", ""),
                    "parent_id": "",
                    "agent_recommendation": d.get("action", ""),
                    "agent_confidence": str(d.get("confidence", 0)),
                    "agent_reasoning": "",
                })

    all_entries.sort(key=lambda x: x.get("decided_at", ""), reverse=True)
    return json.loads(json.dumps(all_entries, default=str))


@router.get("/{document_id}/runs")
def get_document_runs(document_id: str):
    """Get all run entries for a specific document.

    Returns the full runs array — frontend builds tree from parent_id.
    """
    lc = get_lifecycle(document_id)
    if not lc:
        return {"document_id": document_id, "runs": []}

    runs = lc.get("runs", [])
    if isinstance(runs, str):
        try:
            runs = json.loads(runs)
        except (json.JSONDecodeError, TypeError):
            runs = []

    return json.loads(json.dumps({"document_id": document_id, "runs": runs}, default=str))


@router.post("/")
def record_decision(body: DecisionRequest, request: Request):
    """Record a human approval or rejection decision."""
    canonical_action = "HUMAN_APPROVED" if body.action == "APPROVE" else "HUMAN_REJECTED"
    try:
        workflow = get_workflow(body.document_id)
        if workflow or body.workflow_run_id or body.request_version:
            if body.document_type != "PR":
                raise WorkflowConflict("WORKFLOW_DECISION_REQUIRED", "Use a requisition decision for this workflow.")
            decided_by, user_email = _workflow_approver(request)
            decision, applied = record_workflow_decision(
                body.document_id, body.workflow_run_id, body.request_version,
                canonical_action, decided_by, body.justification, approver_email=user_email,
            )
            if applied:
                telemetry.approval_recorded(
                    body.workflow_run_id, canonical_action, decision.get("started_at", ""), decision["completed_at"],
                )
        else:
            user_email = get_user_email(request)
            decided_by = user_email or get_authenticated_user(request) or "unknown"
            decision = record_standalone_decision(
                body.document_id, canonical_action, decided_by, body.justification, body.agent_confidence,
            )
            logger.info("Standalone decision recorded: %s %s", body.action, body.document_type)
    except WorkflowConflict as error:
        raise HTTPException(status_code=409, detail={"code": error.code, "message": str(error)}) from error

    # ERPNext status update for invoice overrides
    if body.document_type == "INVOICE" and body.action == "APPROVE":
        _try_update_invoice_status(body.document_id, user_email=user_email)

    # Stop MR in ERPNext when a human rejects a purchase requisition
    if body.document_type == "PR" and body.action == "REJECT":
        _try_stop_material_request(body.document_id, user_email=user_email)

    return {
        "document_id": body.document_id,
        "action": body.action,
        "decided_by": decision["decided_by"],
        "decided_at": decision["completed_at"],
    }


def _workflow_approver(request: Request) -> tuple[str, str | None]:
    claims = _jwt_claims(request)
    if claims:
        if claims.get("p2p_role") not in {"approver", "procurement", "admin"}:
            raise HTTPException(status_code=403, detail="This role cannot decide a requisition workflow.")
        email = claims.get("email") or claims.get("cognito:email")
        principal = email or claims.get("cognito:username") or claims.get("sub")
        if principal:
            return principal, email
    elif (
        "aws.event" not in request.scope and os.environ.get("DEPLOYMENT_ENV") == "local"
        and os.environ.get("P2P_LOCAL_APPROVER") == "1" and request.client
    ):
        try:
            is_loopback = ip_address(request.client.host).is_loopback
        except ValueError:
            is_loopback = False
        if is_loopback:
            return "local-approver", None
    raise HTTPException(status_code=401, detail="A verified approver identity is required.")


def _try_stop_material_request(mr_id: str, user_email: Optional[str] = None) -> None:
    """Stop the Material Request in ERPNext when a human rejects it."""
    try:
        if not erp_client.is_configured():
            return
        resp = erp_client.request(
            "POST", f"/requisitions/{mr_id}/stop", user_email=user_email, timeout=10,
        )
        if resp.ok:
            logger.info("Material Request %s stopped in ERPNext", mr_id)
        else:
            logger.warning("Failed to stop MR %s: HTTP %s", mr_id, resp.status_code)
    except Exception as e:
        logger.warning("Failed to stop ERPNext MR %s: %s", mr_id, e)


def _try_update_invoice_status(invoice_id: str, user_email: Optional[str] = None) -> None:
    """Update invoice status in ERPNext on override approval."""
    try:
        if not erp_client.is_configured():
            return
        resp = erp_client.request(
            "POST", f"/invoices/{invoice_id}/submit", user_email=user_email, timeout=10,
        )
        if resp.ok:
            logger.info("Invoice %s submitted in ERPNext", invoice_id)
    except Exception as e:
        logger.warning("Failed to update ERPNext for invoice %s: %s", invoice_id, e)
