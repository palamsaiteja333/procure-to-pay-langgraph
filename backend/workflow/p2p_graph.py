# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Checkpointed requisition-to-PO workflow and its AgentCore event contract."""

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Any, Protocol, TypedDict
from uuid import uuid4

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.config import get_stream_writer
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from langgraph_checkpoint_aws import DynamoDBSaver
from opentelemetry.trace import StatusCode

from adapters.models import PurchaseOrderCreate, PurchaseOrderLineItem
from config import settings
from services import lifecycle as audit
from utils import workflow_telemetry as telemetry
from utils.purchase_control import request_version

logger = logging.getLogger("p2p.workflow")
Payload = dict[str, Any]
Emit = Callable[[Payload], None]
BeforePO = Callable[[Payload], str | None]


class WorkflowRuntime(Protocol):
    async def call_tool(self, name: str, arguments: Payload) -> Payload: ...

    async def run_agent(
        self, agent: str, prompt: str, emit: Emit, before_po: BeforePO | None = None,
    ) -> Payload: ...


class WorkflowState(TypedDict, total=False):
    document_id: str
    run_id: str
    request_version: str
    requisition: Payload
    legacy_review: bool
    sourcing: Payload
    requisition_result: Payload
    outcome: str
    error: str
    error_code: str
    po_result: Payload


@asynccontextmanager
async def open_checkpointer() -> AsyncIterator[DynamoDBSaver]:
    """Open the workflow checkpoint table, separate from decisions and audit history."""
    table_name = os.environ.get("P2P_CHECKPOINT_TABLE")
    if not table_name:
        raise RuntimeError("Set P2P_CHECKPOINT_TABLE to the DynamoDB checkpoint table name")
    saver = DynamoDBSaver(
        table_name=table_name,
        region_name=settings.aws_region,
        consistent_read=True,
        enable_checkpoint_compression=True,
    )
    try:
        yield saver
    finally:
        saver.client.close()


def _digest(value: Any) -> str:
    packed = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(packed.encode()).hexdigest()


def _number(value: Any) -> float:
    try:
        number = float(value or 0)
        return number if isfinite(number) else 0
    except (TypeError, ValueError):
        return 0


def _money(value: Any) -> Decimal | None:
    try:
        amount = Decimal(str(value))
        return amount if amount.is_finite() and isfinite(float(amount)) else None
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return None


def _hold_for_review(review: Payload, check: str, status: str, detail: str) -> Payload:
    review.update(recommendation="ESCALATE", auto_approved=False)
    review["findings"].append({"check": check, "status": status, "detail": detail})
    return review


def _line_total(lines: list[Payload], currency: str) -> Decimal | None:
    total = Decimal(0)
    for line in lines:
        if not isinstance(line, dict) or line.get("currency") not in (None, "", currency):
            return None
        quantity = _money(line.get("quantity"))
        rate = _money(line.get("unit_price"))
        if quantity is None or quantity <= 0 or rate is None or rate <= 0:
            return None
        total += quantity * rate
    return total if total > 0 else None


def _supplier(sourcing: Payload) -> tuple[Payload, str, str, Any]:
    vendor = sourcing.get("recommended_vendor") or sourcing.get("recommended_supplier") or {}
    if not isinstance(vendor, dict):
        vendor = {}
    supplier_id = (
        vendor.get("supplier_id") or vendor.get("id") or sourcing.get("recommended_supplier_id")
        or vendor.get("supplier_name") or vendor.get("name")
        or sourcing.get("recommended_supplier") or sourcing.get("vendor_id")
    )
    name = vendor.get("supplier_name") or vendor.get("name") or supplier_id or "unknown"
    score = vendor.get("score") or vendor.get("total_score") or vendor.get("overall_score")
    return vendor, supplier_id if isinstance(supplier_id, str) else "", name, score or "N/A"


def _awards(sourcing: Payload, requisition: Payload) -> dict[str, set[str]]:
    """Resolve the supported one-PO-per-supplier split format."""
    item_ids = {line["item_id"] for line in requisition["line_items"]}
    if not item_ids:
        raise ValueError("The reviewed requisition has no items")
    split = sourcing.get("split_award")
    if not split:
        return {_supplier(sourcing)[1]: item_ids}
    awards: dict[str, set[str]] = {}
    assigned: set[str] = set()
    for allocation in split:
        supplier_id, items = allocation["supplier_id"], set(allocation["items"])
        if not supplier_id or supplier_id in awards or not items or assigned & items:
            raise ValueError("Sourcing returned an invalid split award")
        awards[supplier_id] = items
        assigned.update(items)
    if assigned != item_ids:
        raise ValueError("The split award does not cover the reviewed requisition items")
    return awards


def _quantities(lines: list[Payload]) -> dict[str, Decimal]:
    amounts: dict[str, Decimal] = {}
    for line in lines:
        item_id = line["item_id"]
        quantity = Decimal(str(line["quantity"]))
        if not quantity.is_finite() or quantity <= 0:
            raise ValueError("PO item quantities must be positive")
        amounts[item_id] = amounts.get(item_id, Decimal(0)) + quantity
    return amounts


def _duplicate_rows(lines: list[Payload], duplicate_items: set[str]) -> Counter:
    return Counter(
        (line["item_id"], line["line_number"], Decimal(str(line["quantity"])))
        for line in lines if line["item_id"] in duplicate_items
    )


def _approved_po(document_id: str, supplier_id: str, reviewed_lines: list[Payload], currency: str) -> Payload:
    """Build an ERP write from the reviewed requisition instead of agent-proposed terms."""
    if (
        not isinstance(currency, str) or len(currency) != 3
        or not (currency.isascii() and currency.isalpha() and currency.isupper())
    ):
        raise ValueError("The reviewed requisition has no valid currency")
    lines = []
    for line in reviewed_lines:
        if line.get("currency") not in (None, currency):
            raise ValueError(f"The reviewed currency differs for {line['item_id']}")
        rate = Decimal(str(line.get("unit_price")))
        if not rate.is_finite() or rate <= 0:
            raise ValueError(f"No positive reviewed price for {line['item_id']}")
        if not isinstance(line.get("line_number"), int) or line["line_number"] < 1:
            raise ValueError(f"No reviewed Material Request row number for {line['item_id']}")
        amount = Decimal(str(line["quantity"])) * rate
        lines.append(PurchaseOrderLineItem.model_validate({
            **line, "line_amount": float(amount), "requisition_id": document_id,
        }))
    dates = [line.delivery_date for line in lines if line.delivery_date]
    return {**PurchaseOrderCreate(
        supplier_id=supplier_id, line_items=lines, delivery_date=min(dates, default=None),
    ).model_dump(mode="json", exclude_none=True), "currency": currency}


def _progress(step: str) -> Payload:
    return {"type": "progress", "step": step, "agent": "workflow"}


def _sourcing_progress(sourcing: Payload) -> Payload:
    _, _, name, score = _supplier(sourcing)
    return _progress(f"Step 1 complete: Recommended {name} (Score: {score}/100)")


def _review_progress(result: Payload) -> Payload:
    recommendation = result.get("recommendation", "ESCALATE")
    risk = result.get("risk_level", "UNKNOWN")
    confidence = int(_number(result.get("confidence")) * 100)
    return _progress(f"Step 2 complete: {recommendation} (risk: {risk}, confidence: {confidence}%)")


def _review_summary(review: Payload) -> str:
    return f"{review.get('risk_level', 'UNKNOWN')} risk, total {review.get('total_amount', 0)}"


def result_event(document_id: str, state: WorkflowState) -> Payload:
    outcome = state.get("outcome", "PENDING_APPROVAL")
    sourcing = state.get("sourcing", {})
    review = state.get("requisition_result", {})
    po = state.get("po_result", {})
    vendor, _, _, score = _supplier(sourcing)
    steps = [
        {"agent": name, "result": result}
        for name, result in (("sourcing", sourcing), ("requisition", review), ("po_management", po))
        if result
    ]
    result = {
        "status": {
            "PENDING_APPROVAL": "AWAITING_APPROVAL",
            "PO_CREATED": "COMPLETE",
            "NO_VENDOR": "STOPPED_AT_SOURCING_NO_VENDOR",
        }.get(outcome, outcome),
        "workflow": True,
        "workflow_status": {
            "PO_CREATED": "COMPLETE", "NO_VENDOR": "FAILED",
        }.get(outcome, outcome),
        "workflow_run_id": state.get("run_id", ""),
        "request_version": state.get("request_version", ""),
        "steps": steps,
        "workflow_steps": steps,
        "recommendation": review.get("recommendation", "ESCALATE"),
        "risk_level": review.get("risk_level", "UNKNOWN"),
        "confidence": review.get("confidence", 0),
        "reasoning": review.get("reasoning", ""),
        "findings": review.get("findings", []),
        "auto_approved": bool(review.get("auto_approved")),
        "recommended_vendor": vendor,
        "sourcing_result": {**sourcing, "recommended_vendor": vendor, "vendor_score": score} if sourcing else None,
    }
    if state.get("error"):
        result.update(error=state["error"], error_code=state.get("error_code"))
    if po:
        order_ids = po.get("created_order_ids", [])
        result.update(
            po_result=po,
            created_order_id=po.get("created_order_id", ""),
            created_order_ids=order_ids,
            erp_action_taken=f"PO_CREATED:{','.join(order_ids)}" if order_ids else "NONE",
        )
    return {
        "type": "result", "agent": "workflow", "document_id": document_id,
        "result": result, "tools_used": [], "progress_steps": [step["agent"] for step in steps],
        "metrics": {},
    }


def build_graph(
    runtime: WorkflowRuntime,
    checkpointer: BaseCheckpointSaver,
    lease_owner: str,
    user_email: str,
    apply_policy: Callable[[Payload], Payload],
):
    """Compile the graph for one invocation's runtime and actor context."""
    requested_email = user_email

    async def store(state: WorkflowState, operation, *args, **kwargs):
        return await asyncio.to_thread(
            operation, state["document_id"], state["run_id"], *args,
            lease_owner=lease_owner, **kwargs,
        )

    async def read_request(document_id: str, email: str) -> Payload:
        args = {"requisition_id": document_id}
        if email:
            args["user_email"] = email
        requisition = await runtime.call_tool("erp___get_requisition", args)
        if requisition.get("requisition_id") != document_id or not isinstance(requisition.get("line_items"), list):
            raise ValueError("ERPNext did not return the requested requisition")
        return requisition

    async def failed(state: WorkflowState, error: str, code: str, outcome: str = "FAILED") -> WorkflowState:
        await store(
            state, audit.record_workflow_status,
            "PO_REVIEW" if outcome == "PO_REVIEW" else "FAILED", "po_review" if outcome == "PO_REVIEW" else "failed",
            error=error,
        )
        return {"outcome": outcome, "error": error, "error_code": code}

    async def sourcing(state: WorkflowState) -> WorkflowState:
        emit = get_stream_writer()
        document_id = state["document_id"]
        await store(state, audit.record_workflow_status, "SOURCING", "sourcing")
        emit(_progress("Step 1: Evaluating suppliers..."))
        requisition = await read_request(document_id, requested_email)
        if requisition.get("status") in {"ordered", "rejected", "cancelled", "canceled", "stopped"}:
            return await failed(state, "This requisition is closed to new purchase orders.", "REQUISITION_CLOSED")
        result = await runtime.run_agent(
            "sourcing",
            f"Evaluate suppliers for purchase requisition requisition_id={document_id}."
            f"{identity(requested_email)} Analyze all suppliers and recommend the best with scoring.",
            emit,
        )
        _, vendor_id, name, score = _supplier(result)
        result = {**result, "vendor_id": vendor_id, "vendor_name": name, "vendor_score": _number(score)}
        await store(
            state, audit.record_workflow_analysis, "sourcing", result,
            recommendation=name, confidence=_number(score) / 100, summary=result.get("reasoning", ""),
        )
        emit(_sourcing_progress(result))
        if not vendor_id:
            outcome = await failed(state, "No vendor identified by the Sourcing Agent.", "NO_VENDOR", "NO_VENDOR")
            return {**outcome, "sourcing": result}
        await store(state, audit.record_workflow_status, "SOURCING_COMPLETE", "sourcing_complete")
        return {"sourcing": result, "requisition": requisition, "request_version": request_version(requisition)}

    async def requisition_analysis(state: WorkflowState) -> WorkflowState:
        emit = get_stream_writer()
        await store(state, audit.record_workflow_status, "ANALYZING", "requisition")
        emit(_progress("Step 2: Analyzing requisition — checking items, pricing, budget..."))
        result = state["sourcing"]
        _, vendor_id, _, _ = _supplier(result)
        suppliers = list(dict.fromkeys([
            vendor_id, *(entry["supplier_id"] for entry in result.get("split_award") or []),
        ]))
        document_id = state["document_id"]
        review = await runtime.run_agent(
            "requisition",
            f"Analyze purchase requisition requisition_id={document_id}.{identity(requested_email)} "
            "Use the tools to retrieve the requisition details, check items, suppliers, "
            "look for duplicates, and compare historical pricing. "
            f"The Sourcing Agent has pre-selected these supplier(s): {json.dumps(suppliers)}. "
            "Factor supplier reliability and track record into your risk assessment. "
            "Then provide your recommendation as JSON.",
            emit,
        )
        return {"requisition_result": review}

    async def verify_budget(cost_center: str, currency: str, total: Decimal) -> tuple[str, str]:
        args = {"cost_center": cost_center}
        if requested_email:
            args["user_email"] = requested_email
        try:
            response = await runtime.call_tool("erp___get_budget_status", args)
        except Exception:
            logger.warning("Could not verify a requisition budget", exc_info=True)
            return "WARN", "ERPNext budget verification is unavailable."
        if not isinstance(response, dict) or response.get("error") or not isinstance(response.get("budgets"), list):
            return "WARN", "ERPNext did not return verifiable budget data."
        matches = [row for row in response["budgets"] if isinstance(row, dict) and row.get("cost_center") == cost_center]
        if len(matches) != 1:
            return "WARN", "ERPNext did not return exactly one budget for the requisition cost center."
        budget = matches[0]
        remaining = _money(budget.get("remaining"))
        if budget.get("currency") != currency or remaining is None:
            return "WARN", "ERPNext did not return a valid budget in the requisition currency."
        if budget.get("exceeded") or remaining < total:
            return "FAIL", f"Remaining ERPNext budget of {currency} {remaining:,.2f} does not cover the requisition."
        return "PASS", f"Verified {currency} {remaining:,.2f} remaining budget in ERPNext."

    async def apply_trusted_review(state: WorkflowState, requisition: Payload) -> Payload:
        review = dict(state["requisition_result"])
        model_total = review.get("total_amount")
        model_currency = review.get("currency")
        reported_findings = review.get("findings", [])
        findings = [
            row for row in reported_findings
            if isinstance(row, dict) and isinstance(row.get("check"), str) and isinstance(row.get("status"), str)
        ] if isinstance(reported_findings, list) else []
        total = _money(requisition.get("total_amount"))
        currency = requisition.get("currency")
        review.update(
            total_amount=float(total) if total is not None else None,
            currency=currency,
            findings=findings,
            auto_approved=False,
            automatic_approval_verified=False,
        )
        if review.get("recommendation") == "REJECT":
            return review
        if review.get("error") or findings != reported_findings:
            return _hold_for_review(review, "Requisition Review", "WARN", "Agent review is incomplete.")
        monetary_conflict = (
            (model_total is not None and total is not None and _money(model_total) != total)
            or model_currency not in (None, "", currency)
        )
        if review.get("recommendation") == "APPROVE" and monetary_conflict:
            return _hold_for_review(review, "ERP Requisition", "WARN", "Agent monetary facts differ from ERPNext.")

        review = apply_policy(review)
        if state.get("legacy_review"):
            review["auto_approved"] = False
        if not review.get("auto_approved"):
            return review

        cost_center = requisition.get("cost_center")
        if total is None or total <= 0 or currency != "USD" or not isinstance(cost_center, str) or not cost_center.strip():
            return _hold_for_review(
                review, "ERP Requisition", "WARN", "ERPNext must provide a positive USD total and a cost center.",
            )
        item_total = _line_total(requisition["line_items"], currency)
        if item_total is None or item_total > total:
            return _hold_for_review(
                review, "ERP Requisition", "WARN", "ERPNext line prices and quantities do not support the requisition total.",
            )
        status, detail = await verify_budget(cost_center, currency, total)
        if status != "PASS":
            return _hold_for_review(review, "ERP Budget", status, detail)
        if any(
            row["check"].lower().startswith("budget") and row["status"].upper() in {"WARN", "FAIL"}
            for row in findings
        ):
            return _hold_for_review(review, "ERP Budget", "WARN", "Agent budget findings conflict with ERPNext.")
        review["findings"].append({"check": "ERP Budget", "status": "PASS", "detail": detail})
        review["automatic_approval_verified"] = True
        return review

    async def save_requisition_review(state: WorkflowState, review: Payload) -> None:
        await store(
            state, audit.record_workflow_analysis, "requisition", review,
            recommendation=review.get("recommendation", "ESCALATE"), confidence=_number(review.get("confidence")),
            summary=review.get("reasoning", ""),
        )
        get_stream_writer()(_review_progress(review))

    async def policy_route(state: WorkflowState) -> WorkflowState:
        requisition = await read_request(state["document_id"], requested_email)
        if request_version(requisition) != state["request_version"]:
            review = {**state["requisition_result"], "auto_approved": False, "automatic_approval_verified": False}
            await save_requisition_review(state, review)
            return await failed(state, "The requisition changed during review. Run the workflow again.", "STALE_APPROVAL")
        review = await apply_trusted_review(state, requisition)
        await save_requisition_review(state, review)
        if review.get("recommendation") == "REJECT":
            action, outcome = "AI_REJECTED", "REJECTED"
        elif review.get("auto_approved"):
            action, outcome = "AI_APPROVED", "APPROVED"
        else:
            return {"outcome": "PENDING_APPROVAL", "requisition_result": review}
        justification = (
            review.get("reasoning", "")[:200] if outcome == "REJECTED"
            else _review_summary(review)
        )
        await store(state, audit.record_workflow_gate, state["request_version"], action, justification)
        return {"outcome": outcome, "requisition_result": review}

    async def human_review(state: WorkflowState) -> WorkflowState:
        interrupt({"workflow_run_id": state["run_id"], "request_version": state["request_version"]})
        context = await asyncio.to_thread(audit.get_workflow, state["document_id"])
        decision = context["decision"] if context and context["run_id"] == state["run_id"] else {}
        if decision.get("action") == "HUMAN_REJECTED" and decision.get("request_version") == state["request_version"]:
            return {"outcome": "REJECTED"}
        if (
            decision.get("action") != "HUMAN_APPROVED" or not decision.get("decided_by")
            or decision.get("request_version") != state["request_version"]
        ):
            raise audit.WorkflowConflict("APPROVAL_REQUIRED", "A saved decision is required for this request version.")
        get_stream_writer()(_progress("Human-Approved — proceeding to PO generation"))
        return {"outcome": "APPROVED"}

    async def create_or_recover_po(state: WorkflowState) -> WorkflowState:
        emit = get_stream_writer()
        document_id, run_id = state["document_id"], state["run_id"]
        context = await asyncio.to_thread(audit.get_workflow, document_id)
        decision = context["decision"] if context and context["run_id"] == run_id else {}
        if (
            decision.get("action") not in {"HUMAN_APPROVED", "AI_APPROVED"}
            or not decision.get("decided_by") or decision.get("request_version") != state["request_version"]
        ):
            return await failed(state, "A saved approval is required for this request version.", "APPROVAL_REQUIRED")
        email = requested_email
        if decision["action"] == "HUMAN_APPROVED":
            email = decision.get("approver_email", "")
            local_approval = (
                decision["decided_by"] == "local-approver"
                and os.environ.get("DEPLOYMENT_ENV") == "local"
                and os.environ.get("P2P_LOCAL_APPROVER") == "1"
            )
            if not email and not local_approval:
                return await failed(state, "The saved approval has no verified ERP identity.", "APPROVER_IDENTITY_REQUIRED")
        attempts = set(context["run"].get("po_attempts", []))
        try:
            awards = _awards(state["sourcing"], state["requisition"])
            reviewed_lines = state["requisition"]["line_items"]
            quantities = _quantities(reviewed_lines)
            counts = Counter(line["item_id"] for line in reviewed_lines)
            duplicate_items = {item for item, count in counts.items() if count > 1}
            reviewed_duplicates = _duplicate_rows(reviewed_lines, duplicate_items)
        except (TypeError, ValueError, KeyError, InvalidOperation) as exc:
            return await failed(state, f"Invalid approved award: {exc}", "INVALID_AWARD")
        keys = {
            supplier: _digest(["p2p-po", document_id, run_id, state["request_version"], supplier])
            for supplier in awards
        }
        approval_verified = (
            decision["action"] == "HUMAN_APPROVED"
            or state.get("requisition_result", {}).get("automatic_approval_verified") is True
        )
        verification_error = "The saved automatic approval has no verified ERP budget. Review the request before issuing a PO."

        def approved_arguments(supplier: str) -> Payload:
            lines = [line for line in reviewed_lines if line["item_id"] in awards[supplier]]
            arguments = _approved_po(document_id, supplier, lines, state["requisition"].get("currency"))
            arguments.update(idempotency_key=keys[supplier], approved_request_version=state["request_version"])
            if email:
                arguments["user_email"] = email
            return arguments

        async def reconcile(key: str) -> Payload:
            args = {"idempotency_key": key}
            if email:
                args["user_email"] = email
            try:
                return await runtime.call_tool("erp___reconcile_purchase_order", args)
            except Exception:
                logger.warning("Could not reconcile PO %s", key, exc_info=True)
                return {"status": "unknown"}

        async def needs_review(
            message: str = "ERPNext could not confirm the purchase order outcome. "
            "Reopen the workflow to check for the original order.",
            code: str = "PO_OUTCOME_UNKNOWN",
        ) -> WorkflowState:
            outcome = await failed(state, message, code, "PO_REVIEW")
            confirmed = [receipts[supplier]["order_id"] for supplier in keys if supplier in receipts]
            outcome["po_result"] = {
                "action": "REVIEW", "created_order_id": next(iter(confirmed), ""), "created_order_ids": confirmed,
            } if confirmed else {}
            return outcome

        async def check_current(*, recovering_draft: bool = False) -> WorkflowState | None:
            current = await read_request(document_id, email)
            if request_version(current) != state["request_version"]:
                message = "The requisition changed after approval. Review the current requisition in ERPNext."
                code = "STALE_APPROVAL"
            elif current.get("status") in {"ordered", "rejected", "cancelled", "canceled", "stopped"}:
                message = "This requisition is closed to new purchase orders. Check its orders in ERPNext."
                code = "REQUISITION_CLOSED"
            else:
                return None
            if receipts or recovering_draft:
                return await needs_review(message, code)
            return await failed(state, message, code)

        def matching_order(response: Payload, supplier: str) -> Payload | None:
            order = response.get("purchase_order")
            if not isinstance(order, dict) or not order.get("order_id") or order.get("supplier_id") != supplier:
                return None
            if response.get("status") == "submitted":
                def commercial_lines(lines: list[Payload]) -> Counter:
                    return Counter(
                        (line["requisition_id"], line["item_id"], Decimal(str(line["quantity"])),
                         Decimal(str(line["unit_price"])))
                        for line in lines
                    )

                try:
                    approved = approved_arguments(supplier)
                    if order.get("currency") != approved["currency"] or (
                        commercial_lines(order["line_items"]) != commercial_lines(approved["line_items"])
                    ):
                        return None
                except (KeyError, TypeError, ValueError, ArithmeticError):
                    return None
            return order

        async def recover(attempted: set[str]) -> WorkflowState | None:
            drafts = []
            unresolved = False
            for supplier, key in keys.items():
                if supplier in receipts:
                    continue
                response = await reconcile(key)
                status = response.get("status")
                order = matching_order(response, supplier)
                if status == "submitted" and order:
                    receipts[supplier] = order
                elif status == "draft" and order and key in attempted:
                    drafts.append(supplier)
                elif status != "missing" or key in attempted:
                    unresolved = True
            if unresolved:
                return await needs_review()

            for supplier in drafts:
                if not approval_verified:
                    return await needs_review(verification_error, "APPROVAL_REVIEW_REQUIRED")
                if outcome := await check_current(recovering_draft=True):
                    return outcome
                try:
                    arguments = approved_arguments(supplier)
                except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
                    return await needs_review(f"Could not verify the approved draft: {exc}", "PO_INPUT_REQUIRED")
                if not await asyncio.to_thread(audit.renew_workflow_lease, document_id, run_id, lease_owner):
                    raise audit.WorkflowConflict("WORKFLOW_LEASE_LOST", "This workflow execution lost its lease.")
                arguments["recover_draft"] = True
                try:
                    write = await runtime.call_tool("erp___create_purchase_order", arguments)
                except Exception:
                    logger.warning("Could not confirm draft submission for %s", run_id, exc_info=True)
                    write = {}
                response = await reconcile(keys[supplier])
                if response.get("status") == "submitted" and (order := matching_order(response, supplier)):
                    receipts[supplier] = order
                elif write.get("status") == "rejected":
                    return await needs_review(write.get("error") or "ERPNext rejected the existing draft.", "PO_REJECTED")
                else:
                    return await needs_review()
            return None

        def prepare_po(arguments: Payload) -> str | None:
            if not approval_verified:
                return verification_error
            supplier = arguments.get("supplier_id")
            if supplier not in keys or supplier in receipts:
                return "Only outstanding supplier allocations from the approved award may be created."
            try:
                lines = arguments["line_items"]
                expected = {item: quantities[item] for item in awards[supplier]}
                if _quantities(lines) != expected or any(
                    line.get("requisition_id") not in (None, "", document_id) for line in lines
                ):
                    return "PO lines must match the approved Material Request and supplier allocation."
                expected_rows = Counter({
                    row: count for row, count in reviewed_duplicates.items() if row[0] in awards[supplier]
                })
                if _duplicate_rows(lines, duplicate_items) != expected_rows:
                    return "Duplicate items must refer to their reviewed Material Request line numbers."
                approved = approved_arguments(supplier)
                if not audit.record_po_attempt(document_id, run_id, keys[supplier], lease_owner):
                    return "The PO has already been attempted. Reconcile its outcome with ERPNext."
                arguments.clear()
                arguments.update(approved)
            except (TypeError, ValueError, KeyError, InvalidOperation, audit.WorkflowConflict) as exc:
                return f"Could not authorize this PO tool call: {exc}"
            return None

        async def complete_outstanding() -> WorkflowState | None:
            """Finish a confirmed partial split using its saved approval."""
            for supplier in awards:
                if supplier in receipts:
                    continue
                if outcome := await check_current():
                    return outcome
                supplier_args = {"supplier_id": supplier}
                if email:
                    supplier_args["user_email"] = email
                try:
                    vendor = await runtime.call_tool("erp___get_supplier", supplier_args)
                    if vendor.get("supplier_id") != supplier or vendor.get("status") != "active":
                        return await needs_review(
                            f"Could not confirm that approved supplier {supplier} is active in ERPNext.",
                            "SUPPLIER_UNAVAILABLE",
                        )
                    arguments = approved_arguments(supplier)
                except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
                    return await needs_review(f"Could not prepare the approved allocation for {supplier}: {exc}", "PO_INPUT_REQUIRED")
                except Exception:
                    logger.warning("Could not verify the outstanding supplier %s", supplier, exc_info=True)
                    return await needs_review(f"ERPNext could not verify approved supplier {supplier}.", "SUPPLIER_UNAVAILABLE")
                if error := await asyncio.to_thread(prepare_po, arguments):
                    return await needs_review(error)
                attempts.add(keys[supplier])
                emit(_progress(f"Step 3: Completing the approved purchase order for {supplier}..."))
                response = {}
                try:
                    response = await runtime.call_tool("erp___create_purchase_order", arguments)
                except Exception:
                    logger.warning("PO creation response unavailable for %s", supplier, exc_info=True)
                if outcome := await recover(attempts):
                    if response.get("status") == "rejected" and outcome.get("error_code") == "PO_OUTCOME_UNKNOWN":
                        return await needs_review(
                            response.get("error") or f"ERPNext rejected the approved allocation for {supplier}.",
                            "PO_REJECTED",
                        )
                    return outcome
            return None

        await store(state, audit.record_workflow_status, "PO_GENERATION", "po_generation")
        receipts: dict[str, Payload] = {}
        if outcome := await recover(attempts):
            if outcome.get("error_code") == "PO_OUTCOME_UNKNOWN" and state.get("error_code") == "PO_REJECTED" and state.get("error"):
                return await needs_review(state["error"], "PO_REJECTED")
            return outcome
        if len(receipts) < len(keys) and not approval_verified:
            if attempts or receipts:
                return await needs_review(verification_error, "APPROVAL_REVIEW_REQUIRED")
            return await failed(state, verification_error, "APPROVAL_REVIEW_REQUIRED")
        agent_result: Payload = {}
        if not receipts:
            if outcome := await check_current():
                return outcome
            _, _, _, score = _supplier(state["sourcing"])
            emit(_progress(
                f"Step 3: Generating {len(keys)} purchase orders (split award)..."
                if len(keys) > 1 else "Step 3: Generating purchase order..."
            ))
            allocations = {supplier: sorted(items) for supplier, items in awards.items()}
            prompt = (
                f"Generate purchase orders for approved requisition requisition_id={document_id} "
                f"(sourcing score {score}/100).{identity(email)} Retrieve the requisition, validate items, "
                "and CREATE exactly one PO per listed supplier with only that supplier's allocated items. "
                f"Approved supplier allocations: {json.dumps(allocations)}."
            )
            try:
                agent_result = await runtime.run_agent("po_management", prompt, emit, prepare_po)
            except Exception:
                logger.warning("PO agent execution interrupted for %s", run_id, exc_info=True)
            latest = await asyncio.to_thread(audit.get_workflow, document_id)
            attempts = set(latest["run"].get("po_attempts", []))
            if outcome := await recover(attempts):
                return outcome
            if not receipts:
                return await failed(
                    state, agent_result.get("error") or "The PO agent did not create an order for the approved award.",
                    agent_result.get("error_code") or "PO_NOT_CREATED",
                )

        if outcome := await complete_outstanding():
            return outcome

        order_ids = [receipts[supplier]["order_id"] for supplier in keys]
        total = sum(_number(order.get("total_amount")) for order in receipts.values())
        po_result = {
            **agent_result, "action": "CREATE", "created_order_id": order_ids[0],
            "created_order_ids": order_ids, "total_amount": total,
        }
        po_result.pop("error", None)
        po_result.pop("error_code", None)
        await store(
            state, audit.record_workflow_po, order_ids[0], order_ids, total, "CREATE", po_result,
        )
        if len(order_ids) > 1:
            emit(_progress(f"Purchase Orders {', '.join(order_ids)} created in ERP (split award)"))
        else:
            emit(_progress(f"Purchase Order {order_ids[0]} created in ERP"))
        return {"outcome": "PO_CREATED", "po_result": po_result, "error": "", "error_code": ""}

    async def await_reconciliation(state: WorkflowState) -> WorkflowState:
        interrupt({"workflow_run_id": state["run_id"], "status": "PO_REVIEW"})
        return {}

    def observe(node):
        async def run(state: WorkflowState) -> WorkflowState:
            started = time.perf_counter()
            result = "completed"
            with telemetry.span(f"workflow.{node.__name__}", state["run_id"]) as span:
                try:
                    return await node(state)
                except GraphInterrupt:
                    result = "paused"
                    raise
                except asyncio.CancelledError:
                    result = "cancelled"
                    raise
                except Exception:
                    result = "error"
                    span.set_status(StatusCode.ERROR)
                    raise
                finally:
                    span.set_attribute("aria.outcome", result)
                    telemetry.record(
                        "workflow_step", step=node.__name__, outcome=result,
                        metrics={"StepDurationMs": round((time.perf_counter() - started) * 1000)},
                    )
        return run

    graph = StateGraph(WorkflowState)
    for node in (sourcing, requisition_analysis, policy_route, human_review, create_or_recover_po, await_reconciliation):
        graph.add_node(node.__name__, observe(node))
    graph.add_edge(START, "sourcing")
    graph.add_conditional_edges("sourcing", lambda state: state.get("outcome", ""), {
        "": "requisition_analysis", "FAILED": END, "NO_VENDOR": END,
    })
    graph.add_edge("requisition_analysis", "policy_route")
    approval_routes = {"APPROVED": "create_or_recover_po", "REJECTED": END}
    graph.add_conditional_edges("policy_route", lambda state: state["outcome"], {
        **approval_routes, "PENDING_APPROVAL": "human_review", "FAILED": END,
    })
    graph.add_conditional_edges("human_review", lambda state: state["outcome"], approval_routes)
    graph.add_conditional_edges("create_or_recover_po", lambda state: state["outcome"], {
        "PO_CREATED": END, "FAILED": END, "PO_REVIEW": "await_reconciliation",
    })
    graph.add_edge("await_reconciliation", "create_or_recover_po")
    return graph.compile(checkpointer=checkpointer)


def identity(email: str) -> str:
    return f" Pass user_email={json.dumps(email)} in every ERP tool call." if email else ""


def _terminal_state(context: Payload) -> WorkflowState:
    run_id = context["run_id"]
    entries = {
        entry.get("agent"): entry.get("result") or {}
        for entry in context["runs"] if entry.get("parent_id") == run_id and entry.get("type") == "analysis"
    }
    return {
        "run_id": run_id, "request_version": context["request_version"],
        "sourcing": entries.get("sourcing", {}), "requisition_result": entries.get("requisition", {}),
        "po_result": entries.get("po_management", {}), "outcome": context["status"],
    }


async def stream_workflow(
    document_id: str,
    user_email: str,
    resume_from: str,
    runtime: WorkflowRuntime,
    checkpointer: BaseCheckpointSaver,
    apply_policy: Callable[[Payload], Payload],
) -> AsyncIterator[Payload]:
    """Resolve server-owned state, then stream the graph through the UI's existing contract."""
    owner = str(uuid4())
    context = None
    heartbeat = None
    run_id = ""

    async def release():
        nonlocal heartbeat, context
        if heartbeat is not None:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            heartbeat = None
        claimed, context = context, None
        if claimed and claimed.get("lease_owner") == owner:
            try:
                await asyncio.to_thread(audit.release_workflow_lease, document_id, claimed["run_id"], owner)
            except Exception:
                logger.exception("Could not release workflow lease")

    async def publish_review(state: WorkflowState):
        # Only make approval actionable after LangGraph has durably paused.
        return await asyncio.to_thread(
            audit.record_workflow_gate, document_id, state["run_id"], state["request_version"], "AI_ESCALATED",
            _review_summary(state["requisition_result"]), lease_owner=owner,
        )

    try:
        if not document_id or resume_from not in ("", "po_generation"):
            yield _error("INVALID_WORKFLOW_REQUEST", "A requisition and a valid workflow action are required.")
            return
        context = await asyncio.to_thread(audit.claim_workflow, document_id, owner, resume=bool(resume_from))
        run_id, status = context["run_id"], context["status"]
        if status in {"PO_CREATED", "REJECTED"}:
            yield result_event(document_id, _terminal_state(context))
            return

        async def renew():
            while True:
                await asyncio.sleep(25)
                if not await asyncio.to_thread(audit.renew_workflow_lease, document_id, run_id, owner):
                    raise audit.WorkflowConflict("WORKFLOW_LEASE_LOST", "This workflow execution lost its lease.")

        heartbeat = asyncio.create_task(renew())
        graph = build_graph(runtime, checkpointer, owner, user_email, apply_policy)
        config = {"configurable": {"thread_id": f"{document_id}:{run_id}"}}

        async def get_snapshot():
            with telemetry.span("workflow.checkpoint_read", run_id):
                try:
                    return await graph.aget_state(config)
                except Exception:
                    telemetry.record("checkpoint_read_failed", metrics={"CheckpointFailures": 1})
                    raise

        snapshot = await get_snapshot()
        if not snapshot.values:
            if status in {"PO_GENERATION", "PO_REVIEW"} or context["run"].get("po_attempts"):
                error = "This PO run has no recoverable checkpoint. Reconcile it in ERPNext before creating an order."
                await asyncio.to_thread(
                    audit.record_workflow_status, document_id, run_id, "PO_REVIEW", "po_review",
                    lease_owner=owner, error=error,
                )
                if status != "PO_REVIEW":
                    telemetry.outcome(run_id, "PO_REVIEW", "PO_OUTCOME_UNKNOWN")
                yield result_event(document_id, {
                    **_terminal_state(context), "outcome": "PO_REVIEW", "error": error,
                    "error_code": "PO_OUTCOME_UNKNOWN",
                })
                return
            legacy = status in {"PENDING_APPROVAL", "APPROVED"} and not context["request_version"]
            if context["request_version"]:
                telemetry.record("checkpoint_missing", run_id, metrics={"CheckpointFailures": 1})
                yield _error("CHECKPOINT_MISSING", "The saved workflow checkpoint is unavailable. Review the run before retrying.")
                return
            if resume_from and not legacy:
                yield _error("APPROVAL_REQUIRED", "A saved approval is required before generating a purchase order.")
                return
            graph_input = {"document_id": document_id, "run_id": run_id, "legacy_review": legacy}
            yield _progress("Starting P2P workflow")
        elif not snapshot.next:
            yield result_event(document_id, snapshot.values)
            return
        elif "human_review" in snapshot.next and snapshot.interrupts:
            saved = await publish_review(snapshot.values)
            decision = saved["decision"]
            approved = (
                decision.get("action") == "HUMAN_APPROVED" and decision.get("decided_by")
                and decision.get("request_version") == snapshot.values.get("request_version")
            )
            if not approved:
                await release()
                if resume_from:
                    yield _error("APPROVAL_REQUIRED", "A saved approval is required before generating a purchase order.")
                else:
                    yield result_event(document_id, snapshot.values)
                return
            yield _sourcing_progress(snapshot.values["sourcing"])
            yield _review_progress(snapshot.values["requisition_result"])
            graph_input = Command(resume=True)
        elif "await_reconciliation" in snapshot.next and snapshot.interrupts:
            graph_input = Command(resume=True)
        else:
            if resume_from and status not in {"APPROVED", "PO_GENERATION", "PO_REVIEW"}:
                yield _error("APPROVAL_REQUIRED", "A saved approval is required before generating a purchase order.")
                return
            graph_input = None

        telemetry.record("workflow_resumed" if snapshot.values else "workflow_started", run_id)
        async for event in graph.astream(graph_input, config, stream_mode="custom", durability="sync", version="v1"):
            if heartbeat.done():
                heartbeat.result()
            yield event
        snapshot = await get_snapshot()
        awaiting_approval = "human_review" in snapshot.next and snapshot.interrupts
        if awaiting_approval:
            await publish_review(snapshot.values)
            await release()
        outcome = snapshot.values.get("outcome")
        if outcome and outcome != status:
            telemetry.outcome(run_id, "FAILED" if outcome == "NO_VENDOR" else outcome)
        if awaiting_approval:
            yield _progress("Analysis and sourcing complete — awaiting your decision")
        yield result_event(document_id, snapshot.values)
    except audit.WorkflowConflict as exc:
        if exc.code == "DUPLICATE_WORKFLOW":
            event = result_event(document_id, {"outcome": "RUNNING"})
            event["result"].update(current_step=exc.status, reasoning=str(exc))
            yield event
        else:
            yield _error(exc.code, str(exc), exc.status)
    except Exception:
        logger.exception("Workflow invocation failed for %s", document_id)
        telemetry.record("workflow_error", run_id, metrics={"WorkflowErrors": 1}, error_code="WORKFLOW_ERROR")
        yield _error("WORKFLOW_ERROR", "The workflow was interrupted. Reopen it to recover the saved run.")
    finally:
        await release()


def _error(code: str, message: str, status: str = "") -> Payload:
    return {"type": "error", "error": code, "message": message, "agent": "workflow", "status": status}
