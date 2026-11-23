# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Keep the eval runner honest about independent requirements and PO writes."""

import json
from copy import deepcopy

from evals.__main__ import COST_CENTER, DATASET, run_suite, score


def test_score_finds_wrong_route_missing_fact_and_unexpected_erp_write():
    expected = {
        "status": "PENDING_APPROVAL", "purchase_orders": 0, "required_findings": [["Budget", "FAIL"]],
    }
    observed = {"status": "COMPLETE", "purchase_orders": 1, "findings": [["Budget", "PASS"]]}
    assert set(score(expected, observed)) == set(expected)


def test_replay_exercises_real_workflow_and_reports_each_case():
    report = run_suite()
    profiles = {case["id"]: case for case in report["cases"]}
    assert report["mode"] == "fixture-replay"
    assert report["total"] == len(profiles) == 16
    assert report["passed"] == report["total"], {
        case_id: case["failures"] for case_id, case in profiles.items() if not case["passed"]
    }


def test_direct_budget_lookup_requires_matching_currency_and_unambiguous_balance(tmp_path):
    dataset = json.loads(DATASET.read_text())
    standard = dataset["cases"][0]
    valid_row = {"cost_center": COST_CENTER, "currency": "USD", "remaining": 110, "exceeded": False}
    scenarios = {
        "other-center": [{**valid_row, "cost_center": f"Another {COST_CENTER}"}],
        "other-currency": [{**valid_row, "currency": "CAD"}],
        "ambiguous-budget": [valid_row, {**valid_row, "remaining": 200}],
        "partial-budget": [{key: value for key, value in valid_row.items() if key != "remaining"}],
    }
    cases = []
    for case_id, budgets in scenarios.items():
        case = deepcopy(standard)
        case.update(id=case_id, expected={
            "status": "PENDING_APPROVAL", "decision": "AI_ESCALATED", "budget_checks": 1,
            "purchase_orders": 0, "create_attempts": 0,
        })
        case["erp"]["budgets"] = budgets
        cases.append(case)
    trusted_without_model_finding = deepcopy(standard)
    trusted_without_model_finding["id"] = "trusted-budget-without-model-finding"
    trusted_without_model_finding["erp"]["budgets"] = [valid_row]
    trusted_without_model_finding["replay"]["requisition"]["findings"] = []
    trusted_without_model_finding["expected"].pop("required_findings")
    trusted_without_model_finding["expected"].update(budget_checks=1, create_attempts=1)
    cases.append(trusted_without_model_finding)
    path = tmp_path / "budget-evidence.json"
    path.write_text(json.dumps({"version": dataset["version"], "cases": cases}))

    report = run_suite(path)
    assert report["passed"] == report["total"] == len(cases), report["cases"]


def test_po_replay_uses_reviewed_commercial_terms_instead_of_model_proposal(tmp_path):
    dataset = json.loads(DATASET.read_text())
    case = dataset["cases"][0]
    case["id"] = "changed-po-commercial-terms"
    delivery_date = "2027-01-15"
    warehouse = "Receiving"
    reviewed_line = case["erp"]["lines"][0]
    reviewed_line.update(warehouse=warehouse, delivery_date=delivery_date)
    case["po_proposal"] = {
        "currency": "EUR", "payment_terms": "Pay immediately", "delivery_date": "2027-02-01",
        "approved_request_version": "model-supplied-version", "user_email": "unverified@example.test",
        "recover_draft": True,
        "line_items": [{
            **reviewed_line, "line_number": 1, "unit_price": 1000, "line_amount": 2,
            "warehouse": "Other Warehouse", "delivery_date": "2027-02-01",
        }],
    }
    case["expected"].update(create_attempts=1, authorized_erp_write=True, commercial_terms=[{
        "currency": "USD", "payment_terms": None, "delivery_date": delivery_date, "recover_draft": False,
        "lines": [{"line_number": 1, "line_amount": 110, **reviewed_line}],
    }])
    path = tmp_path / "commercial-terms.json"
    path.write_text(json.dumps({"version": dataset["version"], "cases": [case]}))

    report = run_suite(path)
    assert report["passed"] == report["total"] == 1, report["cases"]
