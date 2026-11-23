# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for ERPNext adapter with mocked client."""

import json
from copy import deepcopy
from datetime import date, timedelta

import pytest
import requests
from unittest.mock import MagicMock, patch
from adapters.erpnext.adapter import DEFAULT_COMPANY, DEFAULT_WAREHOUSE, ERPNextAdapter, PO_IDEMPOTENCY_FIELD
from adapters.erpnext.client import ERPNextClient
from adapters.errors import DuplicatePaymentError, PaymentNotPostedError, PurchaseOrderOutcomeError
from adapters.models import SupplierList, ItemList, PurchaseOrderCreate, PurchaseOrderList, InvoiceList
from utils.purchase_control import request_version


@pytest.fixture
def mock_client():
    """Create a mock ERPNextClient."""
    client = MagicMock()
    return client


@pytest.fixture
def adapter(mock_client):
    """Create adapter with mocked client."""
    return ERPNextAdapter(mock_client)


# --- Supplier Tests ---

class TestListSuppliers:
    def test_returns_canonical_format(self, adapter, mock_client):
        mock_client.get_list.return_value = [
            {"name": "SUP-001", "supplier_name": "Acme Corp", "supplier_group": "Raw Material", "country": "US"},
            {"name": "SUP-002", "supplier_name": "Beta Inc", "supplier_group": "Services", "country": "UK"},
        ]
        result = adapter.list_suppliers()
        assert isinstance(result, SupplierList)
        assert result.total_count == 2
        assert result.suppliers[0].supplier_id == "SUP-001"
        assert result.suppliers[0].supplier_name == "Acme Corp"
        assert result.suppliers[1].supplier_id == "SUP-002"

    def test_filters_by_group(self, adapter, mock_client):
        mock_client.get_list.return_value = []
        adapter.list_suppliers(group="Raw Material")
        mock_client.get_list.assert_called_once()
        call_args = mock_client.get_list.call_args
        assert call_args[1]["filters"] == [["supplier_group", "=", "Raw Material"]]

    def test_empty_list(self, adapter, mock_client):
        mock_client.get_list.return_value = []
        result = adapter.list_suppliers()
        assert result.total_count == 0
        assert result.suppliers == []


class TestGetSupplier:
    def test_returns_canonical_supplier(self, adapter, mock_client):
        mock_client.get.return_value = {
            "name": "SUP-001", "supplier_name": "Acme Corp",
            "country": "US", "default_currency": "USD",
        }
        result = adapter.get_supplier("SUP-001")
        assert result.supplier_id == "SUP-001"
        assert result.supplier_name == "Acme Corp"
        mock_client.get.assert_called_with("Supplier", "SUP-001")


# --- Item Tests ---

class TestListItems:
    def test_returns_canonical_items(self, adapter, mock_client):
        mock_client.get_list.return_value = [
            {"item_code": "BRG-001", "item_name": "Ball Bearing", "item_group": "Bearings", "stock_uom": "Nos"},
        ]
        result = adapter.list_items()
        assert isinstance(result, ItemList)
        assert result.items[0].item_id == "BRG-001"
        assert result.items[0].item_name == "Ball Bearing"

    def test_search_filter(self, adapter, mock_client):
        mock_client.get_list.return_value = []
        adapter.list_items(search="bearing")
        call_args = mock_client.get_list.call_args
        assert any("like" in f for f in call_args[1].get("filters", []))


# --- Purchase Order Tests ---

class TestListPurchaseOrders:
    def test_returns_canonical_orders(self, adapter, mock_client):
        mock_client.get_list.return_value = [
            {"name": "PO-001", "supplier": "SUP-001", "supplier_name": "Acme",
             "status": "To Receive and Bill", "grand_total": 5000.0,
             "transaction_date": "2026-01-15", "currency": "USD"},
        ]
        result = adapter.list_purchase_orders()
        assert isinstance(result, PurchaseOrderList)
        assert result.purchase_orders[0].order_id == "PO-001"
        assert result.purchase_orders[0].supplier_id == "SUP-001"
        assert result.purchase_orders[0].status == "to_receive"
        assert result.purchase_orders[0].total_amount == 5000.0


class TestGetPurchaseOrder:
    def test_returns_with_line_items(self, adapter, mock_client):
        mock_client.get.return_value = {
            "name": "PO-001", "supplier": "SUP-001", "supplier_name": "Acme",
            "status": "To Receive and Bill", "grand_total": 900.0,
            "transaction_date": "2026-01-15", "currency": "USD",
            "items": [
                {"idx": 1, "item_code": "BRG-001", "item_name": "Bearing",
                 "qty": 10, "rate": 45.0, "amount": 450.0, "stock_uom": "Nos"},
                {"idx": 2, "item_code": "BRG-002", "item_name": "Seal",
                 "qty": 20, "rate": 22.5, "amount": 450.0, "stock_uom": "Nos"},
            ],
        }
        result = adapter.get_purchase_order("PO-001")
        assert result.order_id == "PO-001"
        assert len(result.line_items) == 2
        assert result.line_items[0].item_id == "BRG-001"
        assert result.line_items[0].quantity == 10
        assert result.line_items[1].unit_price == 22.5


class TestKeyedPurchaseOrders:
    key = "workflow-001:allocation-001"

    @pytest.fixture
    def request_doc(self):
        due_date = (date.today() + timedelta(days=14)).isoformat()
        return {
            "name": "MR-001", "company": DEFAULT_COMPANY, "docstatus": 1, "status": "Pending",
            "cost_center": "Operations - AMG", "schedule_date": due_date,
            "items": [{
                "idx": 1, "name": "MR-ROW-001", "item_code": "BRG-001", "qty": 1, "rate": 30,
                "uom": "Nos", "warehouse": DEFAULT_WAREHOUSE, "schedule_date": due_date,
                "stock_qty": 1, "ordered_qty": 0,
            }],
        }

    @pytest.fixture
    def order(self, request_doc):
        version = request_version(ERPNextAdapter._map_requisition(request_doc).model_dump(mode="json"))
        return PurchaseOrderCreate(
            supplier_id="SUP-001", idempotency_key=self.key, approved_request_version=version, currency="USD",
            line_items=[{
                "line_number": 1, "item_id": "BRG-001", "quantity": 1,
                "unit_price": 30, "line_amount": 30, "requisition_id": "MR-001",
            }],
        )

    @pytest.fixture
    def erp(self, mock_client, request_doc):
        state = {
            "order": None,
            "supplier": {"name": "SUP-001", "payment_terms": "Net 30"},
            "company": {"name": DEFAULT_COMPANY, "default_currency": "USD"},
        }

        def get(doctype, name, **kwargs):
            return deepcopy({
                "Company": state["company"], "Supplier": state["supplier"],
                "Material Request": request_doc, "Purchase Order": state["order"],
            }[doctype])

        def insert(doctype, payload, **kwargs):
            state["order"] = {
                **deepcopy(payload), "name": "PO-001", "docstatus": 0, "status": "Draft",
                "modified": "2026-11-23 12:34:56.123456",
            }
            for index, item in enumerate(state["order"]["items"], 1):
                item.update(idx=index, amount=item["qty"] * item["rate"])
            return deepcopy(state["order"])

        def submit(doctype, name, *, expected_modified, **kwargs):
            assert expected_modified == state["order"]["modified"]
            state["order"].update({"docstatus": 1, "status": "To Receive and Bill"})
            return deepcopy(state["order"])

        mock_client.get.side_effect = get
        mock_client.get_list.side_effect = lambda *args, **kwargs: [deepcopy(state["order"])] if state["order"] else []
        mock_client.insert.side_effect = insert
        mock_client.submit.side_effect = submit
        return state

    def test_create_inserts_once_with_unique_key_and_mr_row_link(self, adapter, mock_client, order, erp):
        assert adapter.create_purchase_order(order).order_id == "PO-001"
        assert adapter.create_purchase_order(order).order_id == "PO-001"
        mock_client.insert.assert_called_once()
        assert mock_client.insert.call_args.kwargs["retry"] == 0
        assert mock_client.insert.call_args.kwargs["timeout"]
        assert mock_client.insert.call_args.kwargs["allow_redirects"] is False
        payload = mock_client.insert.call_args.args[1]
        assert payload[PO_IDEMPOTENCY_FIELD] == self.key
        assert payload["cost_center"] == payload["items"][0]["cost_center"] == "Operations - AMG"
        assert payload["items"][0]["material_request_item"] == "MR-ROW-001"
        assert payload["transaction_date"] == date.today().isoformat()
        mock_client.submit.assert_called_once()
        assert mock_client.submit.call_args.kwargs["allow_redirects"] is False
        assert mock_client.get_list.call_args.kwargs["filters"] == [[PO_IDEMPOTENCY_FIELD, "=", self.key]]

    @pytest.mark.parametrize("stock_fields", [{"stock_qty": 24}, {}])
    def test_rejects_more_than_remaining_stock_quantity(self, adapter, mock_client, order, erp, request_doc, stock_fields):
        row = request_doc["items"][0]
        row.pop("stock_qty")
        row.update(qty=2, uom="Box", conversion_factor=12, ordered_qty=13, **stock_fields)
        line = order.line_items[0].model_copy(update={"quantity": 2, "unit_of_measure": "Box"})
        version = request_version(ERPNextAdapter._map_requisition(request_doc).model_dump(mode="json"))
        order = order.model_copy(update={"line_items": [line], "approved_request_version": version})

        with pytest.raises(PurchaseOrderOutcomeError, match="exceeds the remaining quantity") as error:
            adapter.create_purchase_order(order)

        assert error.value.rejected
        mock_client.insert.assert_not_called()

    def test_rejects_duplicate_material_request_row(self, adapter, mock_client, order, erp):
        split_order = order.model_copy(update={"line_items": order.line_items * 2})

        with pytest.raises(PurchaseOrderOutcomeError, match="distinct reviewed Material Request rows"):
            adapter.create_purchase_order(split_order)

        mock_client.insert.assert_not_called()

    @pytest.mark.parametrize(("field", "value"), [
        ("quantity", 2), ("unit_price", 29), ("unit_of_measure", "Box"),
        ("warehouse", "Other - AMG"), ("delivery_date", "9999-01-01"),
    ])
    def test_rejects_changed_commercial_line(self, adapter, mock_client, order, erp, field, value):
        changed = order.model_copy(update={"line_items": [order.line_items[0].model_copy(update={field: value})]})
        with pytest.raises(PurchaseOrderOutcomeError, match="differ from the approved Material Request"):
            adapter.create_purchase_order(changed)
        mock_client.insert.assert_not_called()

    @pytest.mark.parametrize(("field", "value", "message"), [
        ("currency", "EUR", "currency does not match"), ("payment_terms", "Net 60", "supplier's default payment terms"),
    ])
    def test_rejects_changed_commercial_header(self, adapter, mock_client, order, erp, field, value, message):
        with pytest.raises(PurchaseOrderOutcomeError, match=message):
            adapter.create_purchase_order(order.model_copy(update={field: value}))
        mock_client.insert.assert_not_called()

    @pytest.mark.parametrize("change_after_insert", [False, True])
    def test_revalidates_requisition_version_before_insert_and_submit(
        self, adapter, mock_client, order, erp, request_doc, change_after_insert,
    ):
        if change_after_insert:
            insert = mock_client.insert.side_effect

            def insert_then_change(*args, **kwargs):
                result = insert(*args, **kwargs)
                request_doc["items"][0]["rate"] = 31
                return result

            mock_client.insert.side_effect = insert_then_change
        else:
            request_doc["items"][0]["rate"] = 31

        with pytest.raises(PurchaseOrderOutcomeError, match="changed after approval") as error:
            adapter.create_purchase_order(order)

        assert error.value.rejected
        assert mock_client.insert.call_count == int(change_after_insert)
        mock_client.submit.assert_not_called()

    def test_rejects_stopped_request_with_unchanged_reviewed_fields(self, adapter, mock_client, order, erp, request_doc):
        request_doc["status"] = "Stopped"
        with pytest.raises(PurchaseOrderOutcomeError, match="closed to new purchase orders"):
            adapter.create_purchase_order(order)
        mock_client.insert.assert_not_called()

    def test_past_due_delivery_rejects_before_insert(self, adapter, mock_client, order, erp, request_doc):
        due_date = (date.today() - timedelta(days=1)).isoformat()
        request_doc["schedule_date"] = request_doc["items"][0]["schedule_date"] = due_date
        version = request_version(ERPNextAdapter._map_requisition(request_doc).model_dump(mode="json"))
        with pytest.raises(PurchaseOrderOutcomeError, match="past-due Material Request delivery date") as error:
            adapter.create_purchase_order(order.model_copy(update={"approved_request_version": version}))
        assert error.value.rejected
        mock_client.insert.assert_not_called()

    def test_lookup_failure_blocks_insert(self, adapter, mock_client, order, erp):
        mock_client.get_list.side_effect = requests.exceptions.ReadTimeout()

        with pytest.raises(PurchaseOrderOutcomeError, match="Could not look up"):
            adapter.create_purchase_order(order)

        mock_client.insert.assert_not_called()

    def test_transport_failure_without_visible_commit_is_unknown(self, adapter, mock_client, order, erp):
        mock_client.insert.side_effect = requests.exceptions.ReadTimeout()

        with pytest.raises(PurchaseOrderOutcomeError, match="insert outcome") as error:
            adapter.create_purchase_order(order)

        assert not error.value.rejected
        mock_client.insert.assert_called_once()
        mock_client.submit.assert_not_called()

    @pytest.mark.parametrize("phase", ["insert", "submit"])
    def test_confirms_committed_order_when_write_response_is_lost(self, adapter, mock_client, order, erp, phase):
        method = getattr(mock_client, phase)
        write = method.side_effect

        def write_then_timeout(*args, **kwargs):
            write(*args, **kwargs)
            raise requests.exceptions.ReadTimeout()

        method.side_effect = write_then_timeout

        assert adapter.create_purchase_order(order).order_id == "PO-001"
        mock_client.insert.assert_called_once()
        mock_client.submit.assert_called_once()

    def test_probe_is_read_only_and_only_explicit_recovery_submits(self, adapter, mock_client, order, erp):
        draft = adapter._approved_po_payload(order)
        mock_client.insert.side_effect("Purchase Order", draft)

        assert adapter.reconcile_purchase_order(self.key).status == "draft"
        with pytest.raises(PurchaseOrderOutcomeError, match="requires checked recovery"):
            adapter.create_purchase_order(order)

        mock_client.insert.assert_not_called()
        mock_client.submit.assert_not_called()
        assert adapter.create_purchase_order(order.model_copy(update={"recover_draft": True})).order_id == "PO-001"
        assert adapter.reconcile_purchase_order(self.key).status == "to_receive"
        mock_client.insert.assert_not_called()
        mock_client.submit.assert_called_once()

    @pytest.mark.parametrize(("field", "value"), [
        ("rate", 1), ("material_request_item", "MR-OTHER-ROW"), ("cost_center", "Other - AMG"),
    ])
    def test_recovery_rejects_draft_with_commercial_drift(self, adapter, mock_client, order, erp, field, value):
        draft = adapter._approved_po_payload(order)
        mock_client.insert.side_effect("Purchase Order", draft)
        erp["order"]["items"][0][field] = value

        with pytest.raises(PurchaseOrderOutcomeError, match="differs from the approved commercial payload"):
            adapter.create_purchase_order(order.model_copy(update={"recover_draft": True}))

        mock_client.insert.assert_not_called()
        mock_client.submit.assert_not_called()

    def test_reconcile_and_recovery_never_insert_if_missing(self, adapter, mock_client, order, erp):
        assert adapter.reconcile_purchase_order(self.key) is None
        with pytest.raises(PurchaseOrderOutcomeError, match="No matching ERPNext purchase order exists"):
            adapter.create_purchase_order(order.model_copy(update={"recover_draft": True}))
        mock_client.insert.assert_not_called()
        mock_client.submit.assert_not_called()

    def test_keyed_lookup_requires_an_explicit_erp_list(self):
        client = ERPNextClient("https://erp.example.com", api_key="test", api_secret="test")
        client.session.get = MagicMock()
        client.session.get.return_value.json.return_value = {}

        with pytest.raises(PurchaseOrderOutcomeError, match="Could not look up"):
            ERPNextAdapter(client).reconcile_purchase_order(self.key)

    def test_keyed_submit_sends_the_checked_draft_timestamp(self):
        client = ERPNextClient("https://erp.example.com", api_key="test", api_secret="test")
        client.session.put = MagicMock()
        client.submit("Purchase Order", "PO-001", expected_modified="2026-11-23 12:34:56.123456")

        body = client.session.put.call_args.kwargs["json"]
        assert json.loads(body["data"]) == {"docstatus": 1, "modified": "2026-11-23 12:34:56.123456"}

    def test_unkeyed_create_keeps_original_api_calls(self, adapter, mock_client, order):
        mock_client.insert.return_value = {"name": "PO-001"}
        mock_client.get.return_value = {"name": "PO-001", "supplier": "SUP-001", "status": "To Receive and Bill"}
        line = order.line_items[0].model_copy(update={"requisition_id": None, "delivery_date": "2026-12-05"})
        unkeyed_order = PurchaseOrderCreate(supplier_id="SUP-001", line_items=[line], delivery_date="2026-12-02")
        assert adapter.create_purchase_order(unkeyed_order).order_id == "PO-001"
        mock_client.get_list.assert_not_called()
        mock_client.submit.assert_called_once_with("Purchase Order", "PO-001")
        payload = mock_client.insert.call_args.args[1]
        assert PO_IDEMPOTENCY_FIELD not in payload
        assert payload["items"][0]["schedule_date"] == "2026-12-02"


class TestBudgetStatus:
    def test_uses_current_fiscal_year_company_currency_and_base_commitments(self, adapter, mock_client):
        year = date.today().year
        starts, ends = f"{year}-01-01", f"{year}-12-31"
        mock_client.get_fiscal_year.return_value = (f"FY {year}", starts, ends)
        mock_client.get.side_effect = [
            {"name": DEFAULT_COMPANY, "default_currency": "CAD"},
            {
                "name": "BUD-01", "company": DEFAULT_COMPANY, "cost_center": "Operations - AMG",
                "fiscal_year": f"FY {year}", "docstatus": 1, "budget_against": "Cost Center",
                "accounts": [{"budget_amount": 20000}],
            },
        ]
        mock_client.get_list.side_effect = [[{"name": "BUD-01"}], [{"total": 12000}]]

        result = adapter.get_budget_status("Operations - AMG")

        assert result.budgets[0].currency == "CAD"
        assert result.budgets[0].remaining == 8000
        budget, spend = mock_client.get_list.call_args_list
        assert ["cost_center", "=", "Operations - AMG"] in budget.kwargs["filters"]
        assert spend.kwargs["fields"] == ["sum(base_grand_total) as total"]
        assert spend.kwargs["filters"] == [
            ["company", "=", DEFAULT_COMPANY], ["docstatus", "=", 1],
            ["cost_center", "=", "Operations - AMG"], ["transaction_date", "between", [starts, ends]],
        ]

    def test_inaccessible_budget_detail_is_not_silently_dropped(self, adapter, mock_client):
        year = date.today().year
        mock_client.get_fiscal_year.return_value = (f"FY {year}", f"{year}-01-01", f"{year}-12-31")
        mock_client.get.side_effect = [
            {"name": DEFAULT_COMPANY, "default_currency": "USD"},
            requests.exceptions.ReadTimeout(),
        ]
        mock_client.get_list.return_value = [{"name": "BUD-01"}]

        with pytest.raises(requests.exceptions.ReadTimeout):
            adapter.get_budget_status("Operations - AMG")


# --- Invoice Tests ---

class TestListInvoices:
    def test_returns_canonical_invoices(self, adapter, mock_client):
        mock_client.get_list.return_value = [
            {"name": "PINV-001", "supplier": "SUP-001", "supplier_name": "Acme",
             "status": "Unpaid", "grand_total": 5000.0, "outstanding_amount": 5000.0,
             "posting_date": "2026-02-01", "currency": "USD"},
        ]
        mock_client.get.return_value = {"items": []}
        result = adapter.list_invoices()
        assert isinstance(result, InvoiceList)
        assert result.invoices[0].invoice_id == "PINV-001"
        assert result.invoices[0].status == "unpaid"


# --- Spend Summary Tests ---

class TestSpendSummary:
    def test_aggregates_counts(self, adapter, mock_client):
        mock_client.get_count.side_effect = [25, 20, 80, 5, 3, 1]  # orders, invoices, suppliers, open, unpaid, overdue
        mock_client.get_list.return_value = [{"total": 150000.0}]
        result = adapter.get_spend_summary()
        assert result.total_orders == 25
        assert result.total_invoices == 20
        assert result.total_suppliers == 80
        assert result.total_spend == 150000.0


# --- Supplier Performance Tests ---

class TestSupplierPerformance:
    def test_returns_performance_data(self, adapter, mock_client):
        mock_client.get_list.return_value = [
            {"supplier": "SUP-001", "supplier_name": "Acme", "order_count": 10, "total_spend": 50000.0},
            {"supplier": "SUP-002", "supplier_name": "Beta", "order_count": 5, "total_spend": 25000.0},
        ]
        result = adapter.get_supplier_performance()
        assert result.total_count == 2
        assert result.suppliers[0].supplier_id == "SUP-001"
        assert result.suppliers[0].total_spend == 50000.0


# --- Payment Tests ---

from adapters.models import PaymentCreate


def _http_error(status: int) -> requests.exceptions.HTTPError:
    resp = requests.Response()
    resp.status_code = status
    return requests.exceptions.HTTPError(f"HTTP {status}", response=resp)


POSTED = {"name": "PE-001", "docstatus": 1, "status": "Submitted", "party": "SUP-001",
          "paid_amount": 2700.0, "mode_of_payment": "Wire Transfer"}
DRAFT = {**POSTED, "docstatus": 0, "status": "Draft"}
PAY = PaymentCreate(supplier_id="SUP-001", amount=2700.0, invoice_id="PINV-001")


class TestCreatePayment:
    """A normal return means posted. Everything else raises."""

    def test_posted_payment_returns_submitted(self, adapter, mock_client):
        mock_client.get_list.return_value = []                                # no existing payment
        mock_client.get.side_effect = [{"outstanding_amount": 2700.0}, POSTED]  # invoice, then re-read
        mock_client.insert.return_value = {"name": "PE-001"}
        mock_client.submit.return_value = {"docstatus": 1}

        result = adapter.create_payment(PAY)

        assert result.payment_id == "PE-001"
        assert result.status == "submitted"
        mock_client.submit.assert_called_once_with("Payment Entry", "PE-001")
        # Allocation uses the invoice's real outstanding amount.
        assert mock_client.insert.call_args[0][1]["references"][0]["allocated_amount"] == 2700.0

    def test_rejected_submit_raises_not_returns_draft(self, adapter, mock_client):
        """Issue #91 point 1: a failed submit used to be a warning and a normal return."""
        mock_client.get_list.return_value = []
        mock_client.get.side_effect = [{"outstanding_amount": 2700.0}, DRAFT]
        mock_client.insert.return_value = {"name": "PE-001"}
        mock_client.submit.side_effect = _http_error(417)  # ERPNext ValidationError

        with pytest.raises(PaymentNotPostedError) as exc:
            adapter.create_payment(PAY)
        assert exc.value.outcome == "rejected"
        assert exc.value.payment_id == "PE-001"

    @pytest.mark.parametrize("failure", [
        requests.exceptions.Timeout("read timed out"),
        requests.exceptions.ConnectionError("connection reset"),
        _http_error(502),
    ])
    def test_indeterminate_submit_is_unknown_not_rejected(self, adapter, mock_client, failure):
        """Issue #91 point 2: timeout/5xx says nothing about whether it posted."""
        mock_client.get_list.return_value = []
        mock_client.get.side_effect = [{"outstanding_amount": 2700.0}, DRAFT]
        mock_client.insert.return_value = {"name": "PE-001"}
        mock_client.submit.side_effect = failure

        with pytest.raises(PaymentNotPostedError) as exc:
            adapter.create_payment(PAY)
        assert exc.value.outcome == "unknown"

    def test_unknown_submit_resolved_by_reread_as_posted(self, adapter, mock_client):
        """The re-read is authoritative: a timed-out submit that did apply is a success."""
        mock_client.get_list.return_value = []
        mock_client.get.side_effect = [{"outstanding_amount": 2700.0}, POSTED]
        mock_client.insert.return_value = {"name": "PE-001"}
        mock_client.submit.side_effect = requests.exceptions.Timeout("read timed out")

        result = adapter.create_payment(PAY)
        assert result.status == "submitted"

    def test_submit_ok_but_entry_still_draft_raises(self, adapter, mock_client):
        mock_client.get_list.return_value = []
        mock_client.get.side_effect = [{"outstanding_amount": 2700.0}, DRAFT]
        mock_client.insert.return_value = {"name": "PE-001"}
        mock_client.submit.return_value = {}

        with pytest.raises(PaymentNotPostedError) as exc:
            adapter.create_payment(PAY)
        assert exc.value.outcome == "unknown"

    def test_reread_failure_after_submit_is_unknown(self, adapter, mock_client):
        mock_client.get_list.return_value = []
        mock_client.get.side_effect = [{"outstanding_amount": 2700.0}, requests.exceptions.ConnectionError("down")]
        mock_client.insert.return_value = {"name": "PE-001"}
        mock_client.submit.return_value = {}

        with pytest.raises(PaymentNotPostedError) as exc:
            adapter.create_payment(PAY)
        assert exc.value.outcome == "unknown"

    def test_invoice_read_failure_aborts_before_insert(self, adapter, mock_client):
        """Issue #91 smaller point: no silent fallback to the caller-supplied amount."""
        mock_client.get_list.return_value = []
        mock_client.get.side_effect = requests.exceptions.ConnectionError("down")

        with pytest.raises(ValueError, match="Cannot read Purchase Invoice"):
            adapter.create_payment(PAY)
        mock_client.insert.assert_not_called()

    def test_insert_conflict_raises(self, adapter, mock_client):
        mock_client.get_list.return_value = []
        mock_client.get.return_value = {"outstanding_amount": 2700.0}
        mock_client.insert.return_value = {}

        with pytest.raises(ValueError, match="Failed to create payment"):
            adapter.create_payment(PAY)
        mock_client.submit.assert_not_called()


class TestCreatePaymentIdempotency:
    """Issue #91 point 3: a retry must never produce a second Payment Entry."""

    def test_existing_posted_payment_blocks_insert(self, adapter, mock_client):
        mock_client.get_list.return_value = [{"name": "PE-001", "docstatus": 1, "status": "Submitted"}]

        with pytest.raises(DuplicatePaymentError) as exc:
            adapter.create_payment(PAY)
        assert exc.value.existing_payment_id == "PE-001"
        assert exc.value.existing_status == "submitted"
        mock_client.insert.assert_not_called()

    def test_existing_draft_blocks_insert(self, adapter, mock_client):
        mock_client.get_list.return_value = [{"name": "PE-001", "docstatus": 0, "status": "Draft"}]

        with pytest.raises(DuplicatePaymentError) as exc:
            adapter.create_payment(PAY)
        assert exc.value.existing_status == "draft"
        assert "unposted draft" in str(exc.value)
        mock_client.insert.assert_not_called()

    def test_posted_reported_over_draft_when_both_exist(self, adapter, mock_client):
        mock_client.get_list.return_value = [
            {"name": "PE-002", "docstatus": 0},
            {"name": "PE-001", "docstatus": 1},
        ]
        with pytest.raises(DuplicatePaymentError) as exc:
            adapter.create_payment(PAY)
        assert exc.value.existing_payment_id == "PE-001"

    def test_dedup_query_targets_invoice_reference_and_excludes_cancelled(self, adapter, mock_client):
        mock_client.get_list.return_value = []
        mock_client.get.side_effect = [{"outstanding_amount": 2700.0}, POSTED]
        mock_client.insert.return_value = {"name": "PE-001"}

        adapter.create_payment(PAY)

        filters = mock_client.get_list.call_args[1]["filters"]
        assert ["Payment Entry Reference", "reference_name", "=", "PINV-001"] in filters
        assert ["docstatus", "<", 2] in filters

    def test_dedup_check_failure_refuses_to_pay(self, adapter, mock_client):
        """'Could not verify' must not become 'paid twice'."""
        mock_client.get_list.side_effect = requests.exceptions.ConnectionError("down")

        with pytest.raises(ValueError, match="Cannot verify existing payments"):
            adapter.create_payment(PAY)
        mock_client.insert.assert_not_called()

    def test_retry_after_unknown_outcome_is_blocked(self, adapter, mock_client):
        """The scenario from the issue end to end: first call times out on submit
        and the re-read shows a draft; the natural retry must be refused."""
        mock_client.get_list.return_value = []
        mock_client.get.side_effect = [{"outstanding_amount": 2700.0}, DRAFT]
        mock_client.insert.return_value = {"name": "PE-001"}
        mock_client.submit.side_effect = requests.exceptions.Timeout("read timed out")
        with pytest.raises(PaymentNotPostedError):
            adapter.create_payment(PAY)

        # Operator / agent retries. The draft from the first attempt now exists.
        mock_client.get_list.return_value = [{"name": "PE-001", "docstatus": 0}]
        with pytest.raises(DuplicatePaymentError):
            adapter.create_payment(PAY)
        assert mock_client.insert.call_count == 1

    def test_no_invoice_id_skips_dedup(self, adapter, mock_client):
        """Unreferenced payments (no invoice) have no idempotency key; unchanged behaviour."""
        mock_client.get.return_value = POSTED
        mock_client.insert.return_value = {"name": "PE-001"}

        result = adapter.create_payment(PaymentCreate(supplier_id="SUP-001", amount=100.0))
        assert result.status == "submitted"
        mock_client.get_list.assert_not_called()
