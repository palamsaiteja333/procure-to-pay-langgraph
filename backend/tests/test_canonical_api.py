# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for canonical P2P API endpoints."""

import pytest
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from adapters.errors import PurchaseOrderOutcomeError
from adapters.models import (
    SupplierList, Supplier, ItemList, Item,
    RequisitionList, Requisition, RequisitionLineItem,
    OrderStatus, PurchaseOrderList, PurchaseOrder, PurchaseOrderLineItem,
    ReceiptList, Receipt,
    InvoiceList, Invoice,
    PaymentList, Payment,
    SpendSummary, SupplierPerformanceList, SupplierPerformance,
)


@pytest.fixture
def mock_adapter():
    """Create a mock ERP adapter."""
    adapter = MagicMock()

    # Default responses
    adapter.list_suppliers.return_value = SupplierList(
        suppliers=[Supplier(supplier_id="SUP-001", supplier_name="Acme Corp")],
        total_count=1,
    )
    adapter.get_supplier.return_value = Supplier(
        supplier_id="SUP-001", supplier_name="Acme Corp", country="US",
    )
    adapter.list_items.return_value = ItemList(
        items=[Item(item_id="ITM-001", item_name="Bearing")],
        total_count=1,
    )
    adapter.get_item.return_value = Item(item_id="ITM-001", item_name="Bearing")
    adapter.list_requisitions.return_value = RequisitionList(requisitions=[], total_count=0)
    adapter.get_requisition.return_value = Requisition(
        requisition_id="MAT-REQ-001",
        line_items=[RequisitionLineItem(line_number=1, item_id="ITM-001", quantity=10)],
    )
    adapter.list_purchase_orders.return_value = PurchaseOrderList(purchase_orders=[], total_count=0)
    adapter.get_purchase_order.return_value = PurchaseOrder(
        order_id="PO-001", supplier_id="SUP-001",
        line_items=[PurchaseOrderLineItem(
            line_number=1, item_id="ITM-001", quantity=10, unit_price=45, line_amount=450,
        )],
    )
    adapter.list_receipts.return_value = ReceiptList(receipts=[], total_count=0)
    adapter.list_invoices.return_value = InvoiceList(invoices=[], total_count=0)
    adapter.get_invoice.return_value = Invoice(
        invoice_id="PINV-001", supplier_id="SUP-001",
    )
    adapter.list_payments.return_value = PaymentList(payments=[], total_count=0)
    adapter.get_spend_summary.return_value = SpendSummary(
        total_spend=50000, total_orders=25, total_invoices=20,
        total_suppliers=10, open_orders=5, unpaid_invoices=3, overdue_invoices=1,
    )
    adapter.get_supplier_performance.return_value = SupplierPerformanceList(
        suppliers=[SupplierPerformance(
            supplier_id="SUP-001", supplier_name="Acme", total_orders=10, total_spend=50000,
        )],
        total_count=1,
    )
    return adapter


@pytest.fixture
def client(mock_adapter):
    """Create FastAPI test client with mocked adapter."""
    with patch("adapters.canonical_api._get_adapter", return_value=mock_adapter):
        from adapters.canonical_api import app
        yield TestClient(app)


class TestHealthEndpoint:
    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestSupplierEndpoints:
    def test_list_suppliers(self, client):
        resp = client.get("/suppliers")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_count"] == 1
        assert data["suppliers"][0]["supplier_id"] == "SUP-001"

    def test_get_supplier(self, client):
        resp = client.get("/suppliers/SUP-001")
        assert resp.status_code == 200
        assert resp.json()["supplier_id"] == "SUP-001"

    def test_get_supplier_not_found(self, client, mock_adapter):
        mock_adapter.get_supplier.side_effect = Exception("Not found")
        resp = client.get("/suppliers/NONEXISTENT")
        assert resp.status_code == 404


class TestItemEndpoints:
    def test_list_items(self, client):
        resp = client.get("/items")
        assert resp.status_code == 200
        assert resp.json()["total_count"] == 1

    def test_get_item(self, client):
        resp = client.get("/items/ITM-001")
        assert resp.status_code == 200
        assert resp.json()["item_id"] == "ITM-001"


class TestRequisitionEndpoints:
    def test_list_requisitions(self, client):
        resp = client.get("/requisitions")
        assert resp.status_code == 200
        assert "requisitions" in resp.json()

    def test_get_requisition(self, client):
        resp = client.get("/requisitions/MAT-REQ-001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["requisition_id"] == "MAT-REQ-001"
        assert len(data["line_items"]) == 1


class TestPurchaseOrderEndpoints:
    keyed_request = {
        "idempotency_key": "workflow:allocation", "supplier_id": "SUP-001", "line_items": [],
        "approved_request_version": "a" * 64, "currency": "USD",
    }

    def test_list_orders(self, client):
        resp = client.get("/purchase-orders")
        assert resp.status_code == 200

    def test_list_orders_with_supplier_filter(self, client, mock_adapter):
        resp = client.get("/purchase-orders?supplier_id=SUP-001")
        assert resp.status_code == 200
        mock_adapter.list_purchase_orders.assert_called_with(supplier_id="SUP-001", status=None)

    def test_get_order(self, client):
        resp = client.get("/purchase-orders/PO-001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["order_id"] == "PO-001"
        assert data["line_items"][0]["item_id"] == "ITM-001"

    def test_unkeyed_create_response(self, client, mock_adapter):
        mock_adapter.create_purchase_order.return_value = mock_adapter.get_purchase_order.return_value

        resp = client.post("/purchase-orders", json={"supplier_id": "SUP-001", "line_items": []})

        assert resp.status_code == 201
        assert resp.json()["order_id"] == "PO-001"

    def test_keyed_create_unknown_response_for_local_mcp(self, client, mock_adapter):
        mock_adapter.create_purchase_order.side_effect = PurchaseOrderOutcomeError("Could not confirm PO")

        resp = client.post("/purchase-orders", json=self.keyed_request)

        assert resp.status_code == 200
        assert resp.json() == {"status": "unknown", "error_code": "PO_OUTCOME_UNKNOWN",
                               "error": "Could not confirm PO"}

    def test_reconcile_response_for_local_mcp(self, client, mock_adapter):
        draft = mock_adapter.get_purchase_order.return_value
        mock_adapter.reconcile_purchase_order.side_effect = [
            None, draft, draft.model_copy(update={"status": OrderStatus.SUBMITTED}),
            PurchaseOrderOutcomeError("Could not confirm PO"),
        ]
        body = {"idempotency_key": "workflow:allocation"}

        assert client.post("/purchase-orders/reconcile", json=body).json() == {"status": "missing"}
        assert client.post("/purchase-orders/reconcile", json=body).json()["status"] == "draft"
        found = client.post("/purchase-orders/reconcile", json=body).json()
        assert found["status"] == "submitted"
        assert found["purchase_order"]["order_id"] == "PO-001"
        assert client.post("/purchase-orders/reconcile", json=body).json()["status"] == "unknown"

    @pytest.mark.parametrize(("path", "method"), [
        ("/purchase-orders", "create_purchase_order"),
        ("/purchase-orders/reconcile", "reconcile_purchase_order"),
    ])
    def test_keyed_po_operations_are_internal(self, client, mock_adapter, monkeypatch, path, method):
        from adapters.canonical_api import Caller, _caller, app

        monkeypatch.setitem(app.dependency_overrides, _caller, lambda: Caller(email="buyer@example.com"))

        resp = client.post(path, json=self.keyed_request)

        assert resp.status_code == 403
        getattr(mock_adapter, method).assert_not_called()

    @pytest.mark.parametrize("tool", ["create_purchase_order", "reconcile_purchase_order"])
    def test_gateway_returns_structured_unknown_outcome(self, client, mock_adapter, tool):
        from adapters.canonical_api import handler

        getattr(mock_adapter, tool).side_effect = PurchaseOrderOutcomeError("Could not confirm PO")
        context = SimpleNamespace(client_context=SimpleNamespace(
            custom={"bedrockAgentCoreToolName": f"erp___{tool}"},
        ))
        event = dict(self.keyed_request)

        assert handler(event, context) == {"status": "unknown", "error_code": "PO_OUTCOME_UNKNOWN",
                                           "error": "Could not confirm PO"}

    def test_keyed_create_requires_approval_version_and_currency(self, client, mock_adapter):
        payload = {key: val for key, val in self.keyed_request.items() if key != "approved_request_version"}
        assert client.post("/purchase-orders", json=payload).status_code == 422
        mock_adapter.create_purchase_order.assert_not_called()

    @pytest.mark.parametrize("operation", ["create_purchase_order", "reconcile_purchase_order"])
    def test_explicit_identity_disables_service_fallback_on_both_transports(self, client, mock_adapter, operation):
        from adapters.canonical_api import Caller, handler

        email = "reviewer@example.com"
        context = SimpleNamespace(client_context=SimpleNamespace(
            custom={"bedrockAgentCoreToolName": f"erp___{operation}"},
        ))
        path = "/purchase-orders" if operation == "create_purchase_order" else "/purchase-orders/reconcile"
        with patch("adapters.canonical_api._get_adapter", side_effect=PermissionError) as get_adapter:
            assert handler({**self.keyed_request, "user_email": email}, context)["status"] == "unknown"
            assert client.post(path, params={"user_email": email}, json=self.keyed_request).json()["status"] == "unknown"

        assert [entry.args[0] for entry in get_adapter.call_args_list] == [Caller(email=email)] * 2
        getattr(mock_adapter, operation).assert_not_called()


class TestReceiptEndpoints:
    def test_list_receipts(self, client):
        resp = client.get("/receipts")
        assert resp.status_code == 200

    def test_list_receipts_by_order(self, client, mock_adapter):
        resp = client.get("/receipts?order_id=PO-001")
        assert resp.status_code == 200
        mock_adapter.list_receipts.assert_called_with(order_id="PO-001")


class TestInvoiceEndpoints:
    def test_list_invoices(self, client):
        resp = client.get("/invoices")
        assert resp.status_code == 200

    def test_get_invoice(self, client):
        resp = client.get("/invoices/PINV-001")
        assert resp.status_code == 200
        assert resp.json()["invoice_id"] == "PINV-001"


class TestPaymentEndpoints:
    def test_list_payments(self, client):
        resp = client.get("/payments")
        assert resp.status_code == 200


class TestAnalyticsEndpoints:
    def test_internal_budget_uses_company_scope_on_both_transports(self, client, mock_adapter):
        from adapters.canonical_api import handler

        context = SimpleNamespace(client_context=SimpleNamespace(
            custom={"bedrockAgentCoreToolName": "erp___get_budget_status"},
        ))
        arguments = {"cost_center": "Operations - AMG", "user_email": "buyer@example.com"}
        service = MagicMock()
        service.get_budget_status.return_value = {"budgets": [], "total_count": 0}
        with patch("adapters.canonical_api._build_service_adapter", return_value=service):
            assert handler(dict(arguments), context) == {"budgets": [], "total_count": 0}
            response = client.get("/analytics/budget-status", params=arguments)
            assert response.json() == {"budgets": [], "total_count": 0}

        mock_adapter.get_budget_status.assert_not_called()
        assert service.get_budget_status.call_count == 2
        service.get_budget_status.assert_called_with(cost_center="Operations - AMG")

    def test_spend_summary(self, client):
        resp = client.get("/analytics/spend-summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_spend"] == 50000
        assert data["total_orders"] == 25
        assert data["overdue_invoices"] == 1

    def test_supplier_performance(self, client):
        resp = client.get("/analytics/supplier-performance")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_count"] == 1
        assert data["suppliers"][0]["supplier_id"] == "SUP-001"


class TestInvoiceExtraction:
    @patch("services.textract.extract_invoice_from_s3")
    def test_extract_invoice_document(self, mock_extract, client):
        """Test POST /invoices/extract calls Textract and returns structured data."""
        mock_extract.return_value = {
            "vendor_name": "Acme Industrial Supply",
            "invoice_number": "INV-2026-0042",
            "invoice_date": "2026-03-15",
            "due_date": "2026-04-14",
            "total_amount": 12345.67,
            "subtotal": 11223.34,
            "tax_amount": 1122.33,
            "po_number": "PUR-ORD-2026-00015",
            "currency": "USD",
            "payment_terms": "Net 30",
            "vendor_address": "123 Industrial Blvd",
            "receiver_name": "P2P Agentic Corp",
            "line_items": [{"ITEM": "Hex Bolt", "QUANTITY": "500"}],
            "confidence": {
                "overall": 97.5,
                "tier": "AUTO_ACCEPT",
                "field_confidences": {},
                "low_confidence_fields": [],
                "missing_fields": [],
            },
            "raw_fields": {},
        }

        resp = client.post("/invoices/extract", json={
            "bucket": "test-bucket",
            "key": "invoices/test-invoice.pdf",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["vendor_name"] == "Acme Industrial Supply"
        assert data["total_amount"] == 12345.67
        assert data["confidence"]["tier"] == "AUTO_ACCEPT"
        assert len(data["line_items"]) == 1

        mock_extract.assert_called_once_with("test-bucket", "invoices/test-invoice.pdf")

    @patch("services.textract.extract_invoice_from_s3")
    def test_extract_invoice_error(self, mock_extract, client):
        """Test extraction failure returns 500."""
        mock_extract.side_effect = Exception("Textract quota exceeded")

        resp = client.post("/invoices/extract", json={
            "bucket": "test-bucket",
            "key": "bad-file.pdf",
        })
        assert resp.status_code == 500
