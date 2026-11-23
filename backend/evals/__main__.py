# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Replay labeled requisitions against the real graph with a simulated ERP."""

import argparse
import asyncio
import hashlib
import inspect
import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import boto3
from langgraph.checkpoint.memory import InMemorySaver
from moto import mock_aws

DATASET = Path(__file__).with_name("requisitions.v1.json")
REQUESTER = "buyer@example.test"
APPROVER = "approver@example.test"
COST_CENTER = "Eval Cost Center"


class FixtureERP:
    def __init__(self, case, agent_outputs):
        self.document_id = case["id"]
        self.snapshot = deepcopy(case["erp"])
        self.outputs = agent_outputs
        self.lose_response = case.get("lose_po_response", False)
        self.po_proposal = case.get("po_proposal", {})
        self.orders = {}
        self.writes = []
        self.budget_checks = 0

    async def call_tool(self, name, arguments):
        if name == "erp___get_requisition":
            assert arguments["requisition_id"] == self.document_id
            assert arguments["user_email"] in {REQUESTER, APPROVER}
            currency = self.snapshot.get("currency", "USD")
            return {
                "requisition_id": self.document_id, "status": self.snapshot.get("status", "submitted"),
                "total_amount": self.snapshot["total_amount"], "cost_center": self.snapshot.get("cost_center", COST_CENTER),
                "currency": currency,
                "line_items": [
                    {"line_number": index, "currency": currency, **deepcopy(line)}
                    for index, line in enumerate(self.snapshot["lines"], 1)
                ],
            }
        if name == "erp___get_budget_status":
            assert arguments == {
                "cost_center": self.snapshot.get("cost_center", COST_CENTER),
                "user_email": REQUESTER,
            }
            self.budget_checks += 1
            budgets = deepcopy(self.snapshot.get("budgets"))
            if budgets is None:
                evidence = self.snapshot["budget"]
                total = self.snapshot["total_amount"]
                budgets = [] if evidence == "UNAVAILABLE" else [{
                    "cost_center": arguments["cost_center"], "currency": "USD",
                    "remaining": total + (100 if evidence == "PASS" else -1),
                    "exceeded": False,
                }]
            return {"budgets": budgets, "total_count": len(budgets)}
        if name == "erp___reconcile_purchase_order":
            order = self.orders.get(arguments["idempotency_key"])
            return {"status": "submitted", "purchase_order": order} if order else {"status": "missing"}
        if name == "erp___get_supplier":
            return {"supplier_id": arguments["supplier_id"], "status": "active"}
        if name == "erp___create_purchase_order":
            return self._submit(arguments)
        raise AssertionError(f"Unexpected ERP tool: {name}")

    def _submit(self, arguments):
        self.writes.append(deepcopy(arguments))
        key = arguments["idempotency_key"]
        if key in self.orders:
            raise AssertionError("Evaluator observed a duplicate ERP write")
        order = {
            "order_id": f"FIXTURE-PO-{len(self.orders) + 1}", "supplier_id": arguments["supplier_id"],
            "total_amount": sum(line["quantity"] * line["unit_price"] for line in arguments["line_items"]),
            "items": {line["item_id"]: line["quantity"] for line in arguments["line_items"]},
            "currency": arguments["currency"], "line_items": deepcopy(arguments["line_items"]),
        }
        self.orders[key] = order
        if self.lose_response:
            raise TimeoutError("Fixture dropped the response after the ERP commit")
        return order

    async def run_agent(self, agent, prompt, emit, before_po=None):
        if agent != "po_management":
            return deepcopy(self.outputs[agent])
        sourcing = self.outputs["sourcing"]
        allocations = sourcing.get("split_award") or [{
            "supplier_id": sourcing["recommended_vendor"]["supplier_id"],
            "items": [line["item_id"] for line in self.snapshot["lines"]],
        }]
        for allocation in allocations:
            arguments = {
                "supplier_id": allocation["supplier_id"],
                "line_items": [
                    {"line_number": index, **deepcopy(line)}
                    for index, line in enumerate(self.snapshot["lines"], 1)
                    if line["item_id"] in allocation["items"]
                ],
                **deepcopy(self.po_proposal),
            }
            if error := before_po(arguments):
                return {"error": error}
            self._submit(arguments)
        return {"action": "CREATE"}


def _evaluate(case, outputs):
    from agentcore_app import _post_process
    from services import lifecycle
    from workflow.p2p_graph import stream_workflow

    erp = FixtureERP(case, outputs)
    saver = InMemorySaver()

    async def invoke(resume_from=""):
        events = [event async for event in stream_workflow(
            case["id"], REQUESTER, resume_from, erp, saver, lambda review: _post_process("requisition", review),
        )]
        result = events[-1]
        return result.get("result") or {"workflow_status": "ERROR", "error_code": result.get("error")}

    async def run():
        result = await invoke()
        initial_status = result["workflow_status"]
        if (action := case.get("human_action")) and initial_status == "PENDING_APPROVAL":
            lifecycle.record_workflow_decision(
                case["id"], result["workflow_run_id"], result["request_version"],
                "HUMAN_APPROVED" if action == "APPROVE" else "HUMAN_REJECTED",
                "fixture-approver", "Synthetic decision", approver_email=APPROVER,
            )
            erp.snapshot.update(case.get("after_decision", {}))
            result = await invoke("po_generation" if action == "APPROVE" else "")
            if case.get("repeat_resume"):
                repeated = await invoke("po_generation")
                if repeated.get("created_order_ids") != result.get("created_order_ids"):
                    raise AssertionError("Repeated resume returned a different order")
        saved = lifecycle.get_workflow(case["id"])
        decision = saved["decision"] if saved else {}
        return {
            "status": result["workflow_status"], "initial_status": initial_status,
            "decision": decision.get("action", ""),
            "error_code": result.get("error_code", ""), "purchase_orders": len(erp.orders),
            "create_attempts": len(erp.writes), "budget_checks": erp.budget_checks,
            "authorized_erp_write": all(
                write.get("approved_request_version") == decision.get("request_version")
                and write.get("user_email") == (APPROVER if decision.get("action") == "HUMAN_APPROVED" else REQUESTER)
                for write in erp.writes
            ),
            "commercial_terms": [{
                "currency": order.get("currency"),
                "payment_terms": order.get("payment_terms"),
                "delivery_date": order.get("delivery_date"),
                "recover_draft": bool(order.get("recover_draft")),
                "lines": [{
                    key: line.get(key) for key in (
                        "line_number", "item_id", "quantity", "unit_price", "line_amount", "warehouse", "delivery_date",
                    )
                } for line in order["line_items"]],
            } for order in erp.writes],
            "suppliers": sorted(order["supplier_id"] for order in erp.orders.values()),
            "allocations": {order["supplier_id"]: order["items"] for order in erp.orders.values()},
            "recommended_supplier": (result.get("recommended_vendor") or {}).get("supplier_id"),
            "findings": [
                [finding.get("check"), finding.get("status")]
                for finding in result.get("findings", []) if isinstance(finding, dict)
            ],
        }

    return asyncio.run(run())


def score(expected, observed):
    failures = {}
    for field, reference in expected.items():
        if field == "required_findings":
            actual = observed.get("findings", [])
            if any(finding not in actual for finding in reference):
                failures[field] = {"expected": reference, "observed": actual}
        elif observed.get(field) != reference:
            failures[field] = {"expected": reference, "observed": observed.get(field)}
    return failures


def run_suite(dataset=DATASET, predictions=None):
    raw = dataset.read_bytes()
    suite = json.loads(raw)
    with mock_aws():
        from agentcore_app import _post_process
        from config import settings
        from services import dynamo
        from workflow import p2p_graph

        resource = boto3.resource("dynamodb", region_name=settings.aws_region)
        resource.create_table(
            TableName=f"{settings.dynamodb_table_prefix}-document-lifecycle",
            KeySchema=[{"AttributeName": "document_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "document_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        reports = []
        with patch.object(dynamo, "dynamodb", resource):
            for case in suite["cases"]:
                expected = case["expected"]
                try:
                    observed = _evaluate(case, predictions[case["id"]] if predictions is not None else case["replay"])
                except Exception as error:
                    observed = {"status": "ERROR", "error_code": type(error).__name__}
                failures = score(expected, observed)
                reports.append({"id": case["id"], "passed": not failures, "failures": failures})
        return {
            "dataset_version": suite["version"], "dataset_sha256": hashlib.sha256(raw).hexdigest(),
            "policy_sha256": hashlib.sha256(inspect.getsource(_post_process).encode()).hexdigest(),
            "workflow_sha256": hashlib.sha256(inspect.getsource(p2p_graph).encode()).hexdigest(),
            "mode": "external-agent-outputs" if predictions is not None else "fixture-replay",
            "passed": sum(case["passed"] for case in reports), "total": len(reports), "cases": reports,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=Path, help="JSON mapping case ID to external sourcing and requisition results")
    parser.add_argument("--model-id", help="Model used to collect the external outputs")
    parser.add_argument("--prompt-version", help="Prompt version used to collect the external outputs")
    args = parser.parse_args()
    if args.predictions and not (args.model_id and args.prompt_version):
        parser.error("--predictions requires --model-id and --prompt-version")
    predictions = json.loads(args.predictions.read_bytes()) if args.predictions else None
    report = run_suite(predictions=predictions)
    if args.predictions:
        report["model_id"] = args.model_id
        report["prompt_version"] = args.prompt_version
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
