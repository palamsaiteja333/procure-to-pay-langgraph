# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Observe real workflow transitions with deterministic agents and test ERP."""

import json

from utils import workflow_telemetry as telemetry

from .test_workflow import CLAIMS, TestERP, approve, check_pending, env, invoke, lifecycle_table


def events(capsys):
    return [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")]


def test_po_review_emits_one_alarm_metric_with_bounded_dimensions(capsys, monkeypatch):
    monkeypatch.setenv("DYNAMODB_TABLE_PREFIX", "staging")
    telemetry.outcome("run-123", "PO_REVIEW", "PO_OUTCOME_UNKNOWN")

    [entry] = events(capsys)
    assert entry["workflow_run_id"] == "run-123"
    assert entry["Deployment"] == "staging"
    assert entry["WorkflowOutcome"] == entry["POReviewEntered"] == 1
    by_metric = {
        metric["Name"]: group["Dimensions"]
        for group in entry["_aws"]["CloudWatchMetrics"] for metric in group["Metrics"]
    }
    assert by_metric == {"WorkflowOutcome": [["Deployment", "Outcome"]], "POReviewEntered": [["Deployment"]]}


def test_one_run_correlates_pause_approval_and_resume(lifecycle_table, env, capsys):
    erp = TestERP(env)
    review = check_pending(invoke(erp))
    assert approve(review).status_code == 200
    assert approve(review).status_code == 200
    invoke(erp, "po_generation")
    invoke(erp, "po_generation")

    entries = events(capsys)
    run_events = [entry for entry in entries if entry.get("workflow_run_id") == review["workflow_run_id"]]
    assert [entry["Outcome"] for entry in run_events if entry["event"] == "workflow_outcome"] == [
        "PENDING_APPROVAL", "PO_CREATED",
    ]
    assert [entry["event"] for entry in run_events if entry["event"] in ("workflow_started", "workflow_resumed")] == [
        "workflow_started", "workflow_resumed",
    ]
    [decision] = [entry for entry in run_events if entry["event"] == "workflow_decision_recorded"]
    assert decision["Outcome"] == "HUMAN_APPROVED"
    assert decision["ApprovalWaitMs"] >= 0
    assert sum(entry["Step"] == "sourcing" for entry in run_events if entry["event"] == "workflow_step") == 1
    assert CLAIMS["email"] not in json.dumps(entries)
    assert erp.order_ids() == ["PO-0001"]
