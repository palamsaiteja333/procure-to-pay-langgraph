# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Document Lifecycle Tracking — normalized schema.

One DDB record per document_id, tracking the full procurement lifecycle.
All fields are NORMALIZED (fixed names, explicit types) — never raw agent JSON.

Status progression (MR):
  CREATED → SOURCING → SOURCING_COMPLETE → ANALYZING → [approval gate]
    ├─ AUTO-APPROVE: → PO_GENERATION → PO_CREATED
    ├─ DEFER TO HUMAN: → PENDING_APPROVAL → APPROVED → PO_GENERATION → PO_CREATED
    └─ AUTO-REJECT: → REJECTED (after Step 2, both sourcing and analysis complete)

Terminal: REJECTED, FAILED

Run tracking:
  All agent runs, workflow executions, and decisions are tracked in a unified
  `runs` array with parent_id linkage for tree structure.
"""

import json
import logging
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional

from boto3.dynamodb.conditions import Attr

from services.dynamo import get_table, get_item, scan_table

logger = logging.getLogger("p2p.lifecycle")

TABLE_NAME = "document-lifecycle"


class Status(str, Enum):
    CREATED = "CREATED"
    ANALYZING = "ANALYZING"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    SOURCING = "SOURCING"
    SOURCING_COMPLETE = "SOURCING_COMPLETE"
    PO_GENERATION = "PO_GENERATION"
    PO_REVIEW = "PO_REVIEW"
    PO_CREATED = "PO_CREATED"
    # Invoice flow
    MATCHING = "MATCHING"
    MATCH_COMPLETE = "MATCH_COMPLETE"
    PENDING_REVIEW = "PENDING_REVIEW"
    PAYMENT_SCHEDULED = "PAYMENT_SCHEDULED"
    PAID = "PAID"
    # Terminal
    REJECTED = "REJECTED"
    FAILED = "FAILED"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _runs(record: dict) -> list[dict]:
    runs = record.get("runs", [])
    return runs if isinstance(runs, list) else []


def _set_run_status(run: dict, status: str) -> None:
    if run.get("status") == status:
        return
    run["status"] = status
    if status == "running":
        run.pop("completed_at", None)
    else:
        run["completed_at"] = _now()


class WorkflowConflict(Exception):
    def __init__(self, code: str, message: str, status: str = ""):
        super().__init__(message)
        self.code = code
        self.status = status


def _safe_float(val) -> float:
    try:
        return float(val or 0)
    except (TypeError, ValueError):
        return 0.0


def _serialize(record: dict) -> dict:
    """Prepare record for DDB — convert floats to Decimal, lists to JSON strings."""
    out = {}
    for k, v in record.items():
        if v is None:
            continue
        if isinstance(v, float):
            out[k] = Decimal(str(v))
        elif isinstance(v, list):
            out[k] = json.dumps(v, default=str)
        elif isinstance(v, dict):
            out[k] = json.dumps(v, default=str)
        else:
            out[k] = v
    return out


def _deserialize(record: dict) -> dict:
    """Restore record from DDB — convert Decimals to float, JSON strings to objects."""
    if not record:
        return record
    out = {}
    for k, v in record.items():
        if isinstance(v, Decimal):
            out[k] = float(v)
        elif k in ("analysis_findings", "decisions", "analysis_runs", "runs", "po_order_ids") and isinstance(v, str):
            try:
                out[k] = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                out[k] = v
        else:
            out[k] = v
    return out


# ─── Core CRUD ──────────────────────────────────────────────────────────────


def get_lifecycle(document_id: str) -> Optional[dict]:
    """Get lifecycle record. Returns None if not found."""
    try:
        item = get_item(TABLE_NAME, {"document_id": document_id})
        return _deserialize(item) if item else None
    except Exception as e:
        logger.warning("get_lifecycle(%s) failed: %s", document_id, e)
        return None


def _mutate_lifecycle(document_id: str, mutate):
    """Apply a serialized change without replacing another writer's lifecycle fields."""
    table = get_table(TABLE_NAME)
    key = {"document_id": document_id}
    for _ in range(8):
        raw = table.get_item(Key=key, ConsistentRead=True).get("Item", {})
        previous = _deserialize(raw)
        base = previous or {
            **key, "document_type": "PR", "status": Status.CREATED.value,
            "created_at": _now(), "runs": [],
        }
        record = deepcopy(base)
        result = mutate(record)
        if record == base and (raw or result is None):
            return result

        updates = {
            name: value for name, value in _serialize(record).items()
            if name != "document_id" and record.get(name) != previous.get(name)
        }
        updates["updated_at"] = _now()
        record["updated_at"] = updates["updated_at"]
        updates["lifecycle_revision"] = int(raw.get("lifecycle_revision", 0)) + 1
        if not raw:
            try:
                table.put_item(Item={**key, **updates}, ConditionExpression=Attr("document_id").not_exists())
                return result
            except table.meta.client.exceptions.ConditionalCheckFailedException:
                continue
        condition = Attr("lifecycle_revision").not_exists()
        if "lifecycle_revision" in raw:
            condition = Attr("lifecycle_revision").eq(raw["lifecycle_revision"])
        for field in ("updated_at", "workflow_lease_owner", "workflow_lease_until"):
            observed = Attr(field).eq(raw[field]) if field in raw else Attr(field).not_exists()
            condition = condition & observed
        names = {f"#field{i}": name for i, name in enumerate(updates)}
        values = {f":update{i}": value for i, value in enumerate(updates.values())}
        expression = ", ".join(f"#field{i} = :update{i}" for i in range(len(updates)))
        try:
            table.update_item(
                Key=key, UpdateExpression=f"SET {expression}", ConditionExpression=condition,
                ExpressionAttributeNames=names, ExpressionAttributeValues=values,
            )
            return result
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            continue
    raise WorkflowConflict("WORKFLOW_CONFLICT", "The lifecycle changed concurrently. Try again.")


def get_lifecycle_by_po(order_id: str) -> Optional[dict]:
    """Find the lifecycle record whose po_order_id matches.

    Used by downstream agents (receiving, invoice matching, payment)
    to find the MR lifecycle record for a given PO.

    Note: Table scan — fine for demo scale. Add GSI on po_order_id for production.
    """
    try:
        items = scan_table(TABLE_NAME)
        for item in items:
            deserialized = _deserialize(item)
            if deserialized.get("po_order_id") == order_id:
                return deserialized
        return None
    except Exception as e:
        logger.warning("get_lifecycle_by_po(%s) failed: %s", order_id, e)
        return None


def _upsert(document_id: str, updates: dict) -> dict:
    """Internal: merge updates into existing record (or create new)."""
    def apply(record):
        record.update(updates)
        return record

    try:
        return _mutate_lifecycle(document_id, apply)
    except Exception as e:
        logger.warning("_upsert(%s) failed: %s", document_id, e)
        return {"document_id": document_id, **updates}


def list_lifecycles(status: Optional[str] = None, *, raise_on_failure: bool = False) -> list[dict]:
    """List all lifecycle records, optionally filtered by status."""
    try:
        items = scan_table(TABLE_NAME)
        if status:
            items = [i for i in items if i.get("status") == status]
        items = [_deserialize(i) for i in items]
        items.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
        return json.loads(json.dumps(items, default=str))
    except Exception as e:
        logger.warning("list_lifecycles failed: %s", e)
        if raise_on_failure:
            raise
        return []


# ─── Typed Step Helpers (normalize agent output) ────────────────────────────


def set_status(document_id: str, status: Status, current_step: str = "") -> dict:
    """Update lifecycle status and current_step."""
    updates = {"status": status.value}
    if current_step:
        updates["current_step"] = current_step
    return _upsert(document_id, updates)


def record_po_created(
    document_id: str,
    order_id: str,
    total_amount: float = 0,
    action: str = "CREATE",
    order_ids: list[str] | None = None,
) -> dict:
    """Record Step 3 results — PO(s) created in ERPNext.

    Supports split awards via order_ids (list of PO IDs).
    order_id is the primary PO; order_ids stores all POs for split awards.
    """
    updates = {
        "status": Status.PO_CREATED.value if order_id else Status.FAILED.value,
        "current_step": "complete",
        "po_action": action,
        "po_order_id": order_id,
        "po_total_amount": total_amount,
        "po_completed_at": _now(),
        "erp_order_id": order_id,
    }
    if order_ids and len(order_ids) > 1:
        updates["po_order_ids"] = order_ids
    return _upsert(document_id, updates)


def record_approval(document_id: str, approved_by: str) -> dict:
    """Record human approval."""
    return _upsert(document_id, {
        "status": Status.APPROVED.value,
        "approved_by": approved_by,
        "approved_at": _now(),
    })


def record_rejection(document_id: str, rejected_by: str) -> dict:
    """Record human rejection."""
    return _upsert(document_id, {
        "status": Status.REJECTED.value,
        "rejected_by": rejected_by,
        "rejected_at": _now(),
    })


def record_failure(document_id: str, error: str) -> dict:
    """Record agent failure."""
    return _upsert(document_id, {
        "status": Status.FAILED.value,
        "error": error[:2000],
    })


# ─── Unified Run Tracking ─────────────────────────────────────────────────


def add_run_entry(
    document_id: str,
    entry_type: str,
    agent: str = "",
    parent_id: str = "",
    status: str = "completed",
    recommendation: str = "",
    confidence: float = 0.0,
    summary: str = "",
    result: dict = None,
    action: str = "",
    decided_by: str = "",
    justification: str = "",
) -> str:
    """Append a run entry to the lifecycle's unified `runs` array.

    This is the ONLY place step results are stored — no duplication at top level.

    Args:
        document_id: The document being processed.
        entry_type: "workflow" | "analysis" | "decision".
        agent: Agent name (workflow, requisition, sourcing, po_management).
        parent_id: Links to parent workflow run ID ("" for top-level).
        status: running, completed, failed, pending_approval, approved, rejected.
        recommendation: Agent recommendation (APPROVE, REJECT, ESCALATE, vendor name).
        confidence: 0.0-1.0.
        summary: Full reasoning/summary text (not truncated).
        result: Full agent result dict (findings, reasoning, vendor details, etc.).
        action: Decision action (AI_APPROVED, HUMAN_REJECTED, etc.).
        decided_by: Who decided (email, AI_AGENT).
        justification: Reason for decision.

    Returns:
        The generated run entry ID (UUID string).
    """
    run_id = str(uuid.uuid4())

    entry = {
        "id": run_id,
        "parent_id": parent_id or None,
        "type": entry_type,
        "agent": agent,
        "status": status,
        "started_at": _now(),
        "completed_at": _now() if status != "running" else None,
        "recommendation": recommendation or None,
        "confidence": _safe_float(confidence) if confidence else None,
        "summary": summary or None,
        "result": result if result else None,
        "action": action or None,
        "decided_by": decided_by or None,
        "justification": justification or None,
    }
    # Strip None values to keep DDB records lean
    entry = {k: v for k, v in entry.items() if v is not None}

    def append(record):
        record["runs"] = [*_runs(record), entry]

    try:
        _mutate_lifecycle(document_id, append)
    except Exception as e:
        logger.warning("add_run_entry(%s) failed: %s", document_id, e)

    return run_id


def update_run_status(document_id: str, run_id: str, status: str) -> None:
    """Update the status of a specific run entry in the runs array."""
    def update(record):
        entry = next((run for run in _runs(record) if run.get("id") == run_id), None)
        if entry:
            _set_run_status(entry, status)

    try:
        _mutate_lifecycle(document_id, update)
    except Exception as e:
        logger.warning("update_run_status(%s, %s) failed: %s", document_id, run_id, e)


def resolve_pending_decision(
    document_id: str,
    action: str,
    decided_by: str,
    justification: str = "",
) -> bool:
    """Resolve a standalone pending decision kept in the legacy audit trail."""
    def update(record):
        target = _standalone_pending_decision(record)
        if _workflow(record) or not target:
            return False
        _apply_human_decision(record, target, action, decided_by, justification)
        return True

    try:
        return _mutate_lifecycle(document_id, update)
    except Exception as e:
        logger.warning("resolve_pending_decision(%s) failed: %s", document_id, e)
        return False


def is_workflow_active(document_id: str, stale_minutes: int = 10) -> dict:
    """Check if a workflow is currently running for this document.

    Returns dict with: active, stale, run_id, status, started_at, current_step.
    A workflow is active if the lifecycle status is in an intermediate running state
    AND there's a workflow run entry with status="running" in runs[].
    If the run started more than stale_minutes ago, it's considered stale (crashed).
    """
    ACTIVE_STATUSES = {
        Status.CREATED.value, Status.SOURCING.value, Status.SOURCING_COMPLETE.value,
        Status.ANALYZING.value, Status.PO_GENERATION.value, Status.MATCHING.value,
    }
    result = {"active": False, "stale": False, "run_id": "", "status": "", "started_at": "", "current_step": ""}

    record = get_lifecycle(document_id)
    if not record:
        return result

    status = record.get("status", "")
    if status not in ACTIVE_STATUSES:
        return result

    result["status"] = status
    result["current_step"] = record.get("current_step", "")

    runs = record.get("runs", [])
    if isinstance(runs, str):
        try:
            runs = json.loads(runs)
        except (json.JSONDecodeError, TypeError):
            # Status says active but runs can't be parsed — treat as stale
            result["active"] = True
            result["stale"] = True
            return result

    # Find the latest workflow run entry with "running" status
    for entry in reversed(runs):
        if entry.get("type") == "workflow" and entry.get("status") == "running":
            result["active"] = True
            result["run_id"] = entry.get("id", "")
            result["started_at"] = entry.get("started_at", "")

            # Check staleness
            started_at = entry.get("started_at", "")
            if started_at:
                try:
                    started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
                    result["stale"] = elapsed > (stale_minutes * 60)
                except (ValueError, TypeError):
                    result["stale"] = True
            else:
                result["stale"] = True
            return result

    # Status is in a running state but no "running" workflow entry — orphaned/stale
    result["active"] = True
    result["stale"] = True
    return result


def is_agent_active(document_id: str, agent_name: str, stale_minutes: int = 5) -> dict:
    """Check if a standalone agent is currently running for this document.

    Checks runs[] for an entry with agent=agent_name and status="running".
    Returns dict with: active, stale, run_id, started_at.
    """
    result = {"active": False, "stale": False, "run_id": "", "started_at": ""}

    record = get_lifecycle(document_id)
    if not record:
        return result

    runs = record.get("runs", [])
    if isinstance(runs, str):
        try:
            runs = json.loads(runs)
        except (json.JSONDecodeError, TypeError):
            return result

    for entry in reversed(runs):
        if entry.get("agent") == agent_name and entry.get("status") == "running":
            result["active"] = True
            result["run_id"] = entry.get("id", "")
            result["started_at"] = entry.get("started_at", "")

            started_at = entry.get("started_at", "")
            if started_at:
                try:
                    started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
                    result["stale"] = elapsed > (stale_minutes * 60)
                except (ValueError, TypeError):
                    result["stale"] = True
            else:
                result["stale"] = True
            return result

    return result


def update_run_entry(
    document_id: str,
    run_id: str,
    status: str = "completed",
    recommendation: str = "",
    confidence: float = 0.0,
    summary: str = "",
    result: dict = None,
) -> None:
    """Update an existing run entry with completion data."""
    def update(record):
        entry = next((run for run in _runs(record) if run.get("id") == run_id), None)
        if entry:
            _set_run_status(entry, status)
            if recommendation:
                entry["recommendation"] = recommendation
            if confidence:
                entry["confidence"] = _safe_float(confidence)
            if summary:
                entry["summary"] = summary
            if result:
                entry["result"] = result

    try:
        _mutate_lifecycle(document_id, update)
    except Exception as e:
        logger.warning("update_run_entry(%s, %s) failed: %s", document_id, run_id, e)


def get_latest_workflow_run_id(document_id: str) -> str:
    """Find the most recent workflow run ID for a document (for resume)."""
    record = get_lifecycle(document_id)
    if not record:
        return ""

    runs = record.get("runs", [])
    if isinstance(runs, str):
        try:
            runs = json.loads(runs)
        except (json.JSONDecodeError, TypeError):
            return ""

    # Find latest workflow entry (reverse search)
    for entry in reversed(runs):
        if entry.get("type") == "workflow":
            return entry.get("id", "")
    return ""


# ─── Approval and execution state for the requisition workflow ──────────────

WORKFLOW_LEASE_SECONDS = 120


def _workflow(record: dict) -> dict | None:
    runs = _runs(record)
    run = next((
        entry for entry in reversed(runs)
        if entry.get("type") == "workflow" and entry.get("agent") == "workflow"
    ), None)
    if run is None:
        return None
    run_id = run["id"]
    decision = next((
        entry for entry in reversed(runs)
        if entry.get("type") == "decision" and entry.get("parent_id") == run_id
    ), {})
    return {
        "run_id": run_id,
        "status": record.get("status", Status.CREATED.value),
        "current_step": record.get("current_step", ""),
        "runs": runs,
        "run": run,
        "decision": decision,
        "request_version": run.get("request_version", ""),
        "lease_owner": record.get("workflow_lease_owner", ""),
        "lease_until": int(record.get("workflow_lease_until", 0)),
        "po_order_id": record.get("po_order_id", ""),
        "po_order_ids": record.get("po_order_ids", []),
        "error": record.get("error", ""),
    }


def get_workflow(document_id: str) -> dict | None:
    """Read the latest requisition workflow and its own decision consistently."""
    raw = get_item(TABLE_NAME, {"document_id": document_id})
    return _workflow(_deserialize(raw)) if raw else None


def _set_workflow_status(record: dict, status: Status, current_step: str) -> None:
    record["status"] = status.value
    record["current_step"] = current_step


def claim_workflow(document_id: str, lease_owner: str, *, resume: bool = False) -> dict:
    """Claim an existing run or create its successor in one conditional update."""
    def claim(record):
        context = _workflow(record)
        status = record.get("status", Status.CREATED.value)
        now = int(time.time())
        if context and status in (Status.PO_CREATED.value, Status.REJECTED.value):
            return context
        if context and context["run"].get("status") == "completed" and context["po_order_id"]:
            return {**context, "status": Status.PO_CREATED.value}
        if context and context["lease_until"] > now and context["lease_owner"] != lease_owner:
            raise WorkflowConflict("DUPLICATE_WORKFLOW", "This workflow is already running.", status)
        if context and status == Status.FAILED.value and context["run"].get("po_attempts"):
            _set_workflow_status(record, Status.PO_REVIEW, "po_review")
            status = Status.PO_REVIEW.value
        can_start_successor = context and status == Status.FAILED.value and not context["run"].get("po_attempts")
        if context is None or can_start_successor:
            if resume:
                raise WorkflowConflict("WORKFLOW_NOT_RESUMABLE", "No resumable workflow was found.", status)
            run_id = str(uuid.uuid4())
            record["runs"] = [*_runs(record), {
                "id": run_id, "type": "workflow", "agent": "workflow",
                "status": "running", "started_at": _now(),
            }]
            _set_workflow_status(record, Status.CREATED, "workflow")
            record["error"] = ""
        else:
            run_id = context["run_id"]
        record["workflow_run_id"] = run_id
        record["workflow_lease_owner"] = lease_owner
        record["workflow_lease_until"] = now + WORKFLOW_LEASE_SECONDS
        return _workflow(record)

    return _mutate_lifecycle(document_id, claim)


def _set_lease(document_id: str, run_id: str, lease_owner: str, *, release: bool) -> bool:
    table = get_table(TABLE_NAME)
    now = int(time.time())
    condition = Attr("workflow_run_id").eq(run_id) & Attr("workflow_lease_owner").eq(lease_owner)
    if not release:
        condition = condition & Attr("workflow_lease_until").gt(now)
    try:
        table.update_item(
            Key={"document_id": document_id},
            UpdateExpression="SET workflow_lease_until = :until, workflow_lease_owner = :owner",
            ExpressionAttributeValues={
                ":until": 0 if release else now + WORKFLOW_LEASE_SECONDS,
                ":owner": "" if release else lease_owner,
            },
            ConditionExpression=condition,
        )
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def renew_workflow_lease(document_id: str, run_id: str, lease_owner: str) -> bool:
    return _set_lease(document_id, run_id, lease_owner, release=False)


def release_workflow_lease(document_id: str, run_id: str, lease_owner: str) -> bool:
    return _set_lease(document_id, run_id, lease_owner, release=True)


def _leased_workflow(record: dict, run_id: str, lease_owner: str) -> dict:
    context = _workflow(record)
    if (
        not context or context["run_id"] != run_id or context["lease_owner"] != lease_owner
        or context["lease_until"] <= int(time.time())
    ):
        raise WorkflowConflict("WORKFLOW_LEASE_LOST", "This workflow execution lost its lease.")
    return context


def record_workflow_status(
    document_id: str, run_id: str, status: Status | str, current_step: str = "", *,
    lease_owner: str, error: str = "",
) -> dict:
    status = Status(status)

    def update(record):
        context = _leased_workflow(record, run_id, lease_owner)
        _set_workflow_status(record, status, current_step)
        if status in (Status.FAILED, Status.PO_REVIEW, Status.REJECTED):
            _set_run_status(context["run"], status.value.lower())
        if error:
            record["error"] = error[:2000]
        return _workflow(record)

    return _mutate_lifecycle(document_id, update)


def _analysis_entry(
    record: dict, run_id: str, agent: str, result: dict, recommendation: str,
    confidence: float, summary: str,
) -> dict:
    runs = _runs(record)
    entry = next((
        run for run in reversed(runs)
        if run.get("type") == "analysis" and run.get("agent") == agent
        and run.get("parent_id") == run_id
    ), None)
    if entry is None:
        now = _now()
        entry = {
            "id": str(uuid.uuid4()), "parent_id": run_id, "type": "analysis",
            "agent": agent, "status": "completed", "started_at": now, "completed_at": now,
        }
        record["runs"] = [*runs, entry]
    entry.update(result=result, recommendation=recommendation, confidence=confidence, summary=summary)
    return entry


def record_workflow_analysis(
    document_id: str, run_id: str, agent: str, result: dict, *, lease_owner: str,
    recommendation: str = "", confidence: float = 0, summary: str = "",
) -> dict:
    def update(record):
        _leased_workflow(record, run_id, lease_owner)
        return _analysis_entry(record, run_id, agent, result, recommendation, confidence, summary)

    return _mutate_lifecycle(document_id, update)


def record_workflow_gate(
    document_id: str, run_id: str, request_version: str, action: str, justification: str = "", *,
    lease_owner: str,
) -> dict:
    status = {
        "AI_ESCALATED": Status.PENDING_APPROVAL, "AI_APPROVED": Status.APPROVED,
        "AI_REJECTED": Status.REJECTED, "FAILED": Status.FAILED,
    }[action]
    if not request_version and status in (Status.PENDING_APPROVAL, Status.APPROVED):
        raise ValueError("Approval requires an analyzed requisition version")

    def update(record):
        context = _leased_workflow(record, run_id, lease_owner)
        decision = context["decision"]
        if decision and decision.get("request_version") == request_version:
            return context
        if decision.get("action") == "AI_ESCALATED" and not decision.get("request_version"):
            entry = decision
        else:
            entry = {
                "id": str(uuid.uuid4()), "parent_id": run_id, "type": "decision",
                "started_at": _now(),
            }
            record["runs"] = [*_runs(record), entry]
        entry.update(
            action=action, decided_by="AI_AGENT", request_version=request_version,
            justification=justification, status=status.value.lower(), completed_at=_now(),
        )
        context["run"]["request_version"] = request_version
        _set_workflow_status(record, status, "approval_gate")
        _set_run_status(context["run"], status.value.lower())
        return _workflow(record)

    return _mutate_lifecycle(document_id, update)


def record_workflow_decision(
    document_id: str, run_id: str, request_version: str, action: str, decided_by: str,
    justification: str, *, approver_email: str | None = None,
) -> tuple[dict, bool]:
    """Resolve exactly the pending decision version displayed to the approver."""
    approved = action == "HUMAN_APPROVED"
    status = Status.APPROVED if approved else Status.REJECTED

    def update(record):
        context = _workflow(record)
        if not context or context["run_id"] != run_id:
            raise WorkflowConflict("STALE_APPROVAL", "The selected workflow run is no longer current.")
        decision = context["decision"]
        if (
            not request_version or request_version != context["request_version"]
            or decision.get("request_version") != request_version
        ):
            raise WorkflowConflict("STALE_APPROVAL", "Open the current analysis and review it again.")
        if decision.get("action") == action:
            return decision, False
        if context["status"] != Status.PENDING_APPROVAL.value or decision.get("action") != "AI_ESCALATED":
            raise WorkflowConflict("APPROVAL_NOT_PENDING", "This workflow has no pending decision.")
        _apply_human_decision(record, decision, action, decided_by, justification)
        if approver_email:
            decision["approver_email"] = approver_email
        _set_run_status(context["run"], status.value.lower())
        return decision, True

    return _mutate_lifecycle(document_id, update)


def _apply_human_decision(record: dict, entry: dict, action: str, decided_by: str, justification: str) -> None:
    approved = action == "HUMAN_APPROVED"
    status = Status.APPROVED if approved else Status.REJECTED
    decided_at = _now()
    entry.update(action=action, status=status.value.lower(), decided_by=decided_by, completed_at=decided_at)
    if justification:
        prior = entry.get("justification", "")
        entry["justification"] = f"{prior} | {decided_by}: {justification}" if prior else justification
    record["status"] = status.value
    record[f"{status.value.lower()}_by"] = decided_by
    record[f"{status.value.lower()}_at"] = decided_at


def record_standalone_decision(
    document_id: str, action: str, decided_by: str, justification: str,
    confidence: float,
) -> dict:
    """Record existing standalone decisions without changing a workflow-owned requisition."""
    def update(record):
        if _workflow(record):
            raise WorkflowConflict("WORKFLOW_DECISION_REQUIRED", "Review the current workflow before deciding.")
        entry = _standalone_pending_decision(record)
        if entry is None:
            action_name = "approve" if action == "HUMAN_APPROVED" else "reject"
            entry = {
                "id": str(uuid.uuid4()), "type": "decision", "agent": "", "started_at": _now(),
                "confidence": confidence,
                "summary": f"Human {action_name}: {justification[:100]}" if justification else f"Human {action_name}",
            }
            record["runs"] = [*_runs(record), entry]
        _apply_human_decision(record, entry, action, decided_by, justification)
        return entry

    return _mutate_lifecycle(document_id, update)


def _standalone_pending_decision(record: dict) -> dict | None:
    return next((
        run for run in reversed(_runs(record))
        if run.get("type") == "decision" and run.get("status") == "pending_approval"
    ), None)


def record_po_attempt(document_id: str, run_id: str, operation_key: str, lease_owner: str) -> bool:
    """Persist the attempt before the ERP request leaves the agent process."""
    def update(record):
        context = _leased_workflow(record, run_id, lease_owner)
        decision = context["decision"]
        if (
            context["status"] not in (Status.APPROVED.value, Status.PO_GENERATION.value)
            or decision.get("action") not in ("AI_APPROVED", "HUMAN_APPROVED")
            or not context["request_version"] or decision.get("request_version") != context["request_version"]
        ):
            raise WorkflowConflict("WORKFLOW_NOT_APPROVED", "This requisition version is not approved for purchase.")
        attempts = context["run"].setdefault("po_attempts", [])
        if operation_key in attempts:
            return False
        attempts.append(operation_key)
        return True

    return _mutate_lifecycle(document_id, update)


def record_workflow_po(
    document_id: str, run_id: str, order_id: str, order_ids: list[str] | None,
    total_amount: float, action: str, result: dict, *, lease_owner: str,
) -> dict:
    def update(record):
        context = _leased_workflow(record, run_id, lease_owner)
        order_result = {**result, "created_order_id": order_id, "created_order_ids": order_ids or [order_id]}
        _analysis_entry(record, run_id, "po_management", order_result, action, 0, result.get("reasoning", ""))
        _set_workflow_status(record, Status.PO_CREATED, "complete")
        _set_run_status(context["run"], "completed")
        record.update(
            po_order_id=order_id, po_order_ids=order_ids or [order_id], po_total_amount=total_amount,
            po_action=action, erp_order_id=order_id, error="",
        )
        if not record.get("po_completed_at"):
            record["po_completed_at"] = _now()
        return _workflow(record)

    return _mutate_lifecycle(document_id, update)
