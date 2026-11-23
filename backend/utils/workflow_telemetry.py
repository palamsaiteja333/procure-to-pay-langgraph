# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Metadata-only workflow telemetry for local logs and CloudWatch."""

import json
import os
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime

from opentelemetry import trace

NAMESPACE = "ARIA/Workflow"
_run_id: ContextVar[str] = ContextVar("workflow_run_id", default="")
_tracer = trace.get_tracer(__name__)


def record(
    event: str, run_id: str = "", *, metrics: dict | None = None,
    step: str = "", outcome: str = "", error_code: str = "", tool: str = "",
) -> None:
    """Emit one JSON log. Only bounded step and outcome names become metric dimensions."""
    message = {"event": event, "Deployment": os.environ.get("DYNAMODB_TABLE_PREFIX", "p2p")}
    if workflow_run_id := run_id or _run_id.get():
        message["workflow_run_id"] = workflow_run_id
    if step:
        message["Step"] = step
    if outcome:
        message["Outcome"] = outcome
    if error_code:
        message["error_code"] = error_code
    if tool:
        message["tool"] = tool
    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid:
        message["trace_id"] = f"{span_context.trace_id:032x}"

    if metrics:
        metric_dimensions = {"StepDurationMs": ("Step",), "WorkflowOutcome": ("Outcome",)}
        groups: dict[tuple, list] = {}
        for name in metrics:
            groups.setdefault(metric_dimensions.get(name, ()), []).append({
                "Name": name, "Unit": "Milliseconds" if name.endswith("Ms") else "Count",
            })
        message.update(metrics)
        message["_aws"] = {
            "Timestamp": time.time_ns() // 1_000_000,
            "CloudWatchMetrics": [{
                "Namespace": NAMESPACE,
                "Dimensions": [["Deployment", *dimensions]],
                "Metrics": definitions,
            } for dimensions, definitions in groups.items()],
        }
    # Keep local stdout available for streamed results and evaluation reports.
    output = sys.stdout if os.environ.get("P2P_EMF_LOGS") == "1" else sys.stderr
    try:
        print(json.dumps(message, separators=(",", ":")), file=output, flush=True)
    except OSError:
        pass  # Telemetry must not prevent a workflow or approval from completing.


def outcome(run_id: str, status: str, error_code: str = "") -> None:
    metrics = {"WorkflowOutcome": 1}
    if status == "PO_REVIEW":
        metrics["POReviewEntered"] = 1
    record("workflow_outcome", run_id, metrics=metrics, outcome=status, error_code=error_code)


def approval_recorded(run_id: str, action: str, started_at: str, completed_at: str) -> None:
    try:
        elapsed_ms = (datetime.fromisoformat(completed_at) - datetime.fromisoformat(started_at)).total_seconds() * 1000
    except (TypeError, ValueError):
        elapsed_ms = None
    record(
        "workflow_decision_recorded", run_id, outcome=action,
        metrics={"ApprovalWaitMs": round(elapsed_ms)} if elapsed_ms is not None and elapsed_ms >= 0 else None,
    )
    if action == "HUMAN_REJECTED":
        outcome(run_id, "REJECTED")


@contextmanager
def span(name: str, run_id: str = ""):
    """Time a child operation without collecting its input, output, or exception message."""
    token = _run_id.set(run_id or _run_id.get())
    try:
        with _tracer.start_as_current_span(
            name, attributes={"aria.workflow_run_id": _run_id.get()},
            record_exception=False, set_status_on_exception=False,
        ) as operation:
            yield operation
    finally:
        _run_id.reset(token)
