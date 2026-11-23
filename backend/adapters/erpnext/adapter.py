# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
ERPNext implementation of ERPAdapterBase.

Translates canonical P2P operations to ERPNext REST API calls.
"""

import io
import logging
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional

import requests

from adapters.erp_adapter_base import ERPAdapterBase
from adapters.errors import DuplicatePaymentError, PaymentNotPostedError, PurchaseOrderOutcomeError
from adapters.models import (
    Supplier, SupplierList,
    Item, ItemList,
    Requisition, RequisitionCreate, RequisitionList, RequisitionLineItem,
    OrderStatus, PurchaseOrder, PurchaseOrderCreate, PurchaseOrderList, PurchaseOrderLineItem,
    Receipt, ReceiptCreate, ReceiptList, ReceiptLineItem,
    Invoice, InvoiceCreate, InvoiceList, InvoiceLineItem,
    Payment, PaymentCreate, PaymentList,
    SpendSummary, SupplierPerformanceList, SupplierPerformance,
    BudgetStatus, BudgetStatusList,
)
from adapters.erpnext.client import ERPNextClient
from adapters.erpnext.field_maps import (
    map_record, map_records, map_status_to_canonical,
    SUPPLIER_TO_CANONICAL, SUPPLIER_LIST_FIELDS,
    ITEM_TO_CANONICAL, ITEM_LIST_FIELDS,
    REQUISITION_TO_CANONICAL, REQUISITION_ITEM_TO_CANONICAL, REQUISITION_LIST_FIELDS,
    PO_TO_CANONICAL, PO_ITEM_TO_CANONICAL, PO_LIST_FIELDS,
    RECEIPT_TO_CANONICAL, RECEIPT_ITEM_TO_CANONICAL, RECEIPT_LIST_FIELDS,
    INVOICE_TO_CANONICAL, INVOICE_ITEM_TO_CANONICAL, INVOICE_LIST_FIELDS,
    PAYMENT_TO_CANONICAL, PAYMENT_LIST_FIELDS,
)
from utils.purchase_control import request_version

logger = logging.getLogger("p2p.adapters.erpnext")

DEFAULT_WAREHOUSE = "Stores - AMG"
DEFAULT_COMPANY = "Apex Manufacturing Group"
DEFAULT_COST_CENTER = "Main - AMG"
DEFAULT_DEPARTMENT = "Operations - AMG"
PO_IDEMPOTENCY_FIELD = "custom_p2p_order_key"
_PO_REQUEST_TIMEOUT = (2, 5)


class ERPNextAdapter(ERPAdapterBase):
    """ERPNext P2P adapter."""

    def __init__(self, client: ERPNextClient):
        self.client = client

    # --- Suppliers ---

    def list_suppliers(self, status: Optional[str] = None,
                       group: Optional[str] = None) -> SupplierList:
        filters = []
        if group:
            filters.append(["supplier_group", "=", group])
        records = self.client.get_list("Supplier", fields=SUPPLIER_LIST_FIELDS,
                                       filters=filters or None)
        suppliers = [Supplier(**map_record(r, SUPPLIER_TO_CANONICAL)) for r in records]
        return SupplierList(suppliers=suppliers, total_count=len(suppliers))

    def get_supplier(self, supplier_id: str) -> Supplier:
        record = self.client.get("Supplier", supplier_id)
        mapped = map_record(record, SUPPLIER_TO_CANONICAL)
        # Get default payment terms
        if record.get("payment_terms"):
            mapped["payment_terms"] = record["payment_terms"]
        # Get primary contact info if available
        if record.get("supplier_primary_contact"):
            mapped["primary_contact_name"] = record.get("supplier_primary_contact")
        return Supplier(**mapped)

    # --- Items ---

    def list_items(self, group: Optional[str] = None,
                   search: Optional[str] = None) -> ItemList:
        filters = []
        or_filters = []
        if group:
            filters.append(["item_group", "=", group])
        if search:
            # Split multi-word searches into individual keyword matches (OR)
            # "hex bolts" → matches items containing "hex" OR "bolts"
            words = [w.strip() for w in search.split() if w.strip()]
            if len(words) == 1:
                filters.append(["item_name", "like", f"%{words[0]}%"])
            else:
                for word in words:
                    or_filters.append(["item_name", "like", f"%{word}%"])
        records = self.client.get_list("Item", fields=ITEM_LIST_FIELDS,
                                       filters=filters or None,
                                       or_filters=or_filters or None)
        items = [Item(**map_record(r, ITEM_TO_CANONICAL)) for r in records]
        return ItemList(items=items, total_count=len(items))

    def get_item(self, item_id: str) -> Item:
        record = self.client.get("Item", item_id)
        return Item(**map_record(record, ITEM_TO_CANONICAL))

    # --- Requisitions (Material Requests) ---

    def list_requisitions(self, status: Optional[str] = None,
                          requester: Optional[str] = None,
                          detail: bool = True) -> RequisitionList:
        filters = [["material_request_type", "=", "Purchase"]]
        if status:
            filters.append(["status", "=", status.replace("_", " ").title()])
        if requester:
            filters.append(["owner", "=", requester])
        records = self.client.get_list("Material Request",
                                       fields=REQUISITION_LIST_FIELDS,
                                       filters=filters,
                                       order_by="creation desc")
        if detail:
            # Enrich each requisition with line items and totals from detail endpoint
            reqs = []
            for r in records:
                try:
                    reqs.append(self.get_requisition(r["name"]))
                except Exception:
                    mapped = map_record(r, REQUISITION_TO_CANONICAL)
                    mapped["status"] = map_status_to_canonical(r.get("status", ""), "Material Request")
                    reqs.append(Requisition(**mapped))
        else:
            # Fast path — basic fields only, no N+1 detail fetches
            reqs = []
            for r in records:
                mapped = map_record(r, REQUISITION_TO_CANONICAL)
                mapped["status"] = map_status_to_canonical(r.get("status", ""), "Material Request")
                reqs.append(Requisition(**mapped))
        return RequisitionList(requisitions=reqs, total_count=len(reqs))

    def get_requisition(self, requisition_id: str) -> Requisition:
        return self._map_requisition(self.client.get("Material Request", requisition_id))

    @staticmethod
    def _map_requisition(record: dict) -> Requisition:
        mapped = map_record(record, REQUISITION_TO_CANONICAL)
        mapped["status"] = map_status_to_canonical(record.get("status", ""), "Material Request")
        # Map line items
        items = []
        for item in record.get("items", []):
            line = map_record(item, REQUISITION_ITEM_TO_CANONICAL)
            items.append(RequisitionLineItem(**line))
        mapped["line_items"] = items
        # Calculate total from items if not available
        if not mapped.get("total_amount"):
            mapped["total_amount"] = sum(
                (li.unit_price or 0) * li.quantity for li in items
            )
        return Requisition(**mapped)

    def create_requisition(self, data: RequisitionCreate) -> Requisition:
        from datetime import date, timedelta
        default_date = (date.today() + timedelta(days=14)).isoformat()
        schedule = data.required_date or default_date
        # ERPNext rejects schedule_date before today
        if schedule < date.today().isoformat():
            schedule = default_date

        items = []
        for li in data.line_items:
            item = {
                "item_code": li.item_id,
                "qty": li.quantity,
                "schedule_date": schedule,
                "warehouse": li.warehouse or DEFAULT_WAREHOUSE,
            }
            if li.unit_price:
                item["rate"] = li.unit_price
            items.append(item)

        mr_data = {
            "material_request_type": "Purchase",
            "schedule_date": schedule,
            "company": DEFAULT_COMPANY,
            "cost_center": data.cost_center or DEFAULT_COST_CENTER,
            "department": data.department or DEFAULT_DEPARTMENT,
            "items": items,
        }

        doc = self.client.insert("Material Request", mr_data)
        if doc and doc.get("name"):
            self.client.submit("Material Request", doc["name"])
            return self.get_requisition(doc["name"])
        raise ValueError("Failed to create requisition in ERPNext")

    def stop_requisition(self, requisition_id: str):
        """Stop a Material Request (marks as rejected/stopped in ERPNext)."""
        try:
            resp = self.client.session.post(
                f"{self.client.base_url}/api/method/frappe.client.set_value",
                json={
                    "doctype": "Material Request",
                    "name": requisition_id,
                    "fieldname": "status",
                    "value": "Stopped",
                },
            )
            if resp.ok:
                logger.info(f"Stopped MR {requisition_id}")
            else:
                logger.warning(f"Failed to stop MR {requisition_id}: {resp.status_code}")
        except Exception as e:
            logger.warning(f"Failed to stop MR {requisition_id}: {e}")

    # --- Purchase Orders ---

    def list_purchase_orders(self, supplier_id: Optional[str] = None,
                             status: Optional[str] = None) -> PurchaseOrderList:
        # Map canonical status back to ERPNext status(es) for filtering
        CANONICAL_TO_ERPNEXT_PO = {
            "to_receive": ["To Receive and Bill", "To Receive"],
            "to_bill": ["To Bill"],
            "partially_received": ["Partially Received"],
            "completed": ["Completed"],
            "received": ["Delivered"],
            "draft": ["Draft"],
            "cancelled": ["Cancelled", "Closed"],
        }
        filters = []
        if supplier_id:
            filters.append(["supplier", "=", supplier_id])
        if status:
            erp_statuses = CANONICAL_TO_ERPNEXT_PO.get(status)
            if erp_statuses:
                filters.append(["status", "in", erp_statuses])
            else:
                filters.append(["status", "=", status.replace("_", " ").title()])
        records = self.client.get_list("Purchase Order", fields=PO_LIST_FIELDS,
                                       filters=filters or None,
                                       order_by="transaction_date desc")
        orders = []
        for r in records:
            mapped = map_record(r, PO_TO_CANONICAL)
            mapped["status"] = map_status_to_canonical(r.get("status", ""), "Purchase Order")
            # Enrich with line items from detail
            try:
                detail = self.client.get("Purchase Order", r["name"])
                items = [PurchaseOrderLineItem(**map_record(item, PO_ITEM_TO_CANONICAL))
                         for item in detail.get("items", [])]
                mapped["line_items"] = items
            except Exception:
                mapped["line_items"] = []
            orders.append(PurchaseOrder(**mapped))
        return PurchaseOrderList(purchase_orders=orders, total_count=len(orders))

    def get_purchase_order(self, order_id: str) -> PurchaseOrder:
        return self._map_purchase_order(self.client.get("Purchase Order", order_id))

    @staticmethod
    def _map_purchase_order(record: dict) -> PurchaseOrder:
        mapped = map_record(record, PO_TO_CANONICAL)
        mapped["status"] = map_status_to_canonical(record.get("status", ""), "Purchase Order")
        items = []
        for item in record.get("items", []):
            line = map_record(item, PO_ITEM_TO_CANONICAL)
            items.append(PurchaseOrderLineItem(**line))
        mapped["line_items"] = items
        return PurchaseOrder(**mapped)

    @staticmethod
    def _check_keyed_po_quantity(line: PurchaseOrderLineItem, request_row: dict, *, submitting: bool) -> float:
        try:
            request_qty = Decimal(str(request_row.get("qty") or 0))
            stock_value = request_row.get("stock_qty")
            stock_qty = Decimal(str(stock_value)) if stock_value is not None else None
            conversion = request_row.get("conversion_factor")
            if conversion is not None:
                factor = Decimal(str(conversion))
            elif stock_qty is not None and request_qty > 0:
                factor = stock_qty / request_qty
            else:
                factor = Decimal(1)
            requested_stock = stock_qty if stock_qty is not None else request_qty * factor
            ordered_stock = Decimal(str(request_row.get("ordered_qty") or request_row.get("received_qty") or 0))
            proposed_stock = Decimal(str(line.quantity)) * factor
            if not all(value.is_finite() for value in (factor, requested_stock, ordered_stock, proposed_stock)):
                raise ValueError("Non-finite quantity")
            if factor <= 0 or ordered_stock < 0 or proposed_stock <= 0:
                raise ValueError("Invalid conversion or quantity")
        except (InvalidOperation, ValueError) as exc:
            raise PurchaseOrderOutcomeError(
                f"Could not verify the remaining quantity for {line.item_id} on Material Request "
                f"{line.requisition_id}", rejected=True,
            ) from exc

        # ERPNext tracks ordered_qty in stock units, while the approved quantity uses the MR row's UOM.
        if submitting and proposed_stock > requested_stock - ordered_stock:
            raise PurchaseOrderOutcomeError(
                f"The order exceeds the remaining quantity for {line.item_id} on Material Request "
                f"{line.requisition_id} (line {request_row.get('idx', line.line_number)})", rejected=True,
            )
        return float(factor)

    def create_purchase_order(self, data: PurchaseOrderCreate) -> PurchaseOrder:
        if data.idempotency_key:
            return self._create_keyed_purchase_order(data)

        from datetime import date, timedelta
        default_date = (date.today() + timedelta(days=14)).isoformat()

        # Build MR item name lookup if we have a requisition link
        # ERPNext needs material_request_item (row name) for proper MR→PO linkage
        mr_item_map: dict[str, str] = {}
        mr_ids = set(li.requisition_id for li in data.line_items if li.requisition_id)
        for mr_id in mr_ids:
            try:
                mr = self.client.get("Material Request", mr_id)
                for mr_item in mr.get("items", []):
                    mr_item_map[mr_item.get("item_code", "")] = mr_item.get("name", "")
            except Exception as e:
                logger.warning(f"Could not fetch MR {mr_id} for item linkage: {e}")

        items = []
        for li in data.line_items:
            item = {
                "item_code": li.item_id,
                "qty": li.quantity,
                "rate": li.unit_price,
                "schedule_date": data.delivery_date or li.delivery_date or default_date,
                "warehouse": li.warehouse or DEFAULT_WAREHOUSE,
            }
            if li.requisition_id:
                item["material_request"] = li.requisition_id
                if li.item_id in mr_item_map:
                    item["material_request_item"] = mr_item_map[li.item_id]
            items.append(item)

        # Map common payment term abbreviations to ERPNext template names
        PAYMENT_TERMS_MAP = {
            "NT15": "Net 15", "NT30": "Net 30", "NT45": "Net 45", "NT60": "Net 60",
            "N30": "Net 30", "N45": "Net 45", "N60": "Net 60",
            "2/10N30": "2/10 Net 30", "2/10NT30": "2/10 Net 30",
            "1/15N45": "1/15 Net 45", "1/15NT45": "1/15 Net 45",
        }
        resolved_terms = PAYMENT_TERMS_MAP.get(
            (data.payment_terms or "").replace(" ", ""),
            data.payment_terms
        ) if data.payment_terms else None

        po_data = {
            "supplier": data.supplier_id,
            "transaction_date": data.delivery_date or default_date,
            "schedule_date": data.delivery_date or default_date,
            "company": DEFAULT_COMPANY,
            "items": items,
        }
        if resolved_terms:
            po_data["payment_terms_template"] = resolved_terms

        doc = self.client.insert("Purchase Order", po_data)
        if doc and doc.get("name"):
            self.client.submit("Purchase Order", doc["name"])
            # Update linked Material Requests to "Ordered" status
            # ERPNext requires material_request_item for auto-update, which we
            # may not have. Manually set the MR per_ordered to trigger status change.
            mr_ids = set()
            for li in data.line_items:
                if li.requisition_id:
                    mr_ids.add(li.requisition_id)
            for mr_id in mr_ids:
                try:
                    self._update_mr_ordered_status(mr_id)
                except Exception as e:
                    logger.warning(f"Failed to update MR {mr_id} status: {e}")
            return self.get_purchase_order(doc["name"])
        raise ValueError("Failed to create purchase order in ERPNext")

    def _approved_po_payload(self, data: PurchaseOrderCreate, *, submitting: bool = True) -> dict:
        request_ids = {line.requisition_id for line in data.line_items}
        if len(request_ids) != 1 or None in request_ids or "" in request_ids:
            raise PurchaseOrderOutcomeError("A keyed order must link to one Material Request", rejected=True)
        request_id = next(iter(request_ids))
        try:
            supplier = self.client.get("Supplier", data.supplier_id, timeout=_PO_REQUEST_TIMEOUT)
            company = self.client.get("Company", DEFAULT_COMPANY, timeout=_PO_REQUEST_TIMEOUT)
            request = self.client.get("Material Request", request_id, timeout=_PO_REQUEST_TIMEOUT)
            canonical = self._map_requisition(request)
        except Exception as exc:
            raise PurchaseOrderOutcomeError("Could not verify the Material Request, supplier and currency in ERPNext") from exc
        if (
            request.get("name") != request_id
            or request.get("company") != DEFAULT_COMPANY
            or request_version(canonical.model_dump(mode="json")) != data.approved_request_version
        ):
            raise PurchaseOrderOutcomeError("The Material Request changed after approval", rejected=True)
        if submitting and (request.get("docstatus") != 1 or request.get("status") in {"Ordered", "Stopped", "Cancelled"}):
            raise PurchaseOrderOutcomeError("The Material Request is closed to new purchase orders", rejected=True)
        if supplier.get("name") != data.supplier_id or supplier.get("disabled") or supplier.get("prevent_pos"):
            raise PurchaseOrderOutcomeError("The approved supplier is not active for purchase orders", rejected=True)

        currency = request.get("currency") or company.get("default_currency")
        if not currency or data.currency != currency:
            raise PurchaseOrderOutcomeError("The approved order currency does not match ERPNext", rejected=True)
        terms = supplier.get("payment_terms") or ""
        if data.payment_terms and data.payment_terms != terms:
            raise PurchaseOrderOutcomeError("Use the ERPNext supplier's default payment terms", rejected=True)

        items = []
        request_rows = {row.get("idx"): row for row in request.get("items", [])}
        seen = set()
        for line in data.line_items:
            row = request_rows.get(line.line_number, {})
            if line.line_number in seen or row.get("item_code") != line.item_id or not row.get("name"):
                raise PurchaseOrderOutcomeError("PO lines must match distinct reviewed Material Request rows", rejected=True)
            seen.add(line.line_number)
            warehouse = row.get("warehouse") or DEFAULT_WAREHOUSE
            uom = row.get("uom") or "Nos"
            schedule = row.get("schedule_date") or request.get("schedule_date")
            if (
                line.quantity != row.get("qty") or line.unit_price != row.get("rate") or line.unit_price <= 0
                or line.unit_of_measure != uom or line.warehouse not in (None, warehouse)
                or not schedule or line.delivery_date not in (None, schedule)
            ):
                raise PurchaseOrderOutcomeError("PO lines differ from the approved Material Request", rejected=True)
            factor = self._check_keyed_po_quantity(line, row, submitting=submitting)
            items.append({
                "item_code": line.item_id, "qty": line.quantity, "rate": line.unit_price,
                "material_request": request_id, "material_request_item": row["name"],
                "warehouse": warehouse, "uom": uom, "conversion_factor": factor, "schedule_date": schedule,
            })
            if canonical.cost_center:
                items[-1]["cost_center"] = canonical.cost_center

        schedule = min(item["schedule_date"] for item in items)
        if data.delivery_date and data.delivery_date != schedule:
            raise PurchaseOrderOutcomeError("The PO delivery date differs from the approved Material Request", rejected=True)
        payload = {
            "supplier": data.supplier_id, "company": DEFAULT_COMPANY, "currency": currency,
            "schedule_date": schedule, "items": items,
            PO_IDEMPOTENCY_FIELD: data.idempotency_key,
        }
        if canonical.cost_center:
            payload["cost_center"] = canonical.cost_center
        if terms:
            payload["payment_terms_template"] = terms
        return payload

    def _create_keyed_purchase_order(self, data: PurchaseOrderCreate) -> PurchaseOrder:
        key = data.idempotency_key
        existing = self._find_keyed_purchase_order(key)
        if existing:
            record = self._read_keyed_purchase_order(key, existing["name"])
            if record["docstatus"] == 0 and not data.recover_draft:
                raise PurchaseOrderOutcomeError("A keyed ERPNext draft exists and requires checked recovery")
            return self._confirm_keyed_purchase_order(data, record)
        if data.recover_draft:
            raise PurchaseOrderOutcomeError("No matching ERPNext purchase order exists to recover")

        payload = self._approved_po_payload(data)
        transaction_date = date.today().isoformat()
        if payload["schedule_date"] < transaction_date:
            raise PurchaseOrderOutcomeError("Refresh and approve the past-due Material Request delivery date", rejected=True)
        payload["transaction_date"] = transaction_date
        try:
            self.client.insert("Purchase Order", payload, retry=0, timeout=_PO_REQUEST_TIMEOUT, allow_redirects=False)
        except Exception as exc:
            logger.warning("Keyed PO insert needs reconciliation: %s", type(exc).__name__)
        existing = self._find_keyed_purchase_order(key)
        if not existing:
            raise PurchaseOrderOutcomeError("ERPNext PO insert outcome could not be confirmed")
        record = self._read_keyed_purchase_order(key, existing["name"])
        return self._confirm_keyed_purchase_order(data, record)

    def reconcile_purchase_order(self, idempotency_key: str) -> Optional[PurchaseOrder]:
        existing = self._find_keyed_purchase_order(idempotency_key)
        if not existing:
            return None
        record = self._read_keyed_purchase_order(idempotency_key, existing["name"])
        order = self._map_purchase_order(record)
        return order.model_copy(update={"status": OrderStatus.DRAFT}) if record["docstatus"] == 0 else order

    def _find_keyed_purchase_order(self, key: str) -> Optional[dict]:
        try:
            records = self.client.get_list(
                "Purchase Order",
                fields=["name", "docstatus", "supplier", PO_IDEMPOTENCY_FIELD],
                filters=[[PO_IDEMPOTENCY_FIELD, "=", key]],
                limit=2,
                timeout=_PO_REQUEST_TIMEOUT,
                require_data=True,
            )
        except Exception as e:
            raise PurchaseOrderOutcomeError("Could not look up the PO key in ERPNext") from e
        if not records:
            return None
        if len(records) != 1 or records[0].get(PO_IDEMPOTENCY_FIELD) != key or not records[0].get("name"):
            raise PurchaseOrderOutcomeError("ERPNext returned an invalid or ambiguous PO key match")
        return records[0]

    def _read_keyed_purchase_order(self, key: str, name: str) -> dict:
        try:
            record = self.client.get("Purchase Order", name, timeout=_PO_REQUEST_TIMEOUT)
        except Exception as exc:
            raise PurchaseOrderOutcomeError(f"Could not read keyed ERPNext PO {name}") from exc
        if (
            record.get("name") != name or record.get(PO_IDEMPOTENCY_FIELD) != key
            or record.get("docstatus") not in (0, 1)
        ):
            raise PurchaseOrderOutcomeError(f"ERPNext PO {name} is not a matching draft or submitted order")
        return record

    @staticmethod
    def _check_persisted_po(expected: dict, record: dict) -> None:
        fields = ["supplier", "company", "currency", "payment_terms_template", "schedule_date"]
        line_fields = ["material_request", "material_request_item", "item_code", "uom", "warehouse", "schedule_date"]
        if expected.get("cost_center"):
            fields.append("cost_center")
            line_fields.append("cost_center")

        def lines(data: dict) -> Counter:
            return Counter(
                tuple(line.get(field) for field in line_fields)
                + tuple(Decimal(str(line.get(field))) for field in ("qty", "rate", "conversion_factor"))
                for line in data.get("items", [])
            )

        try:
            matches = all((expected.get(field) or "") == (record.get(field) or "") for field in fields)
            if matches and lines(expected) == lines(record):
                return
        except (InvalidOperation, KeyError, TypeError, ValueError):
            pass
        raise PurchaseOrderOutcomeError("Persisted ERPNext order differs from the approved commercial payload", rejected=True)

    def _confirm_keyed_purchase_order(self, data: PurchaseOrderCreate, record: dict) -> PurchaseOrder:
        needs_submit = record["docstatus"] == 0
        expected = self._approved_po_payload(data, submitting=needs_submit)
        self._check_persisted_po(expected, record)
        if not needs_submit:
            return self._map_purchase_order(record)

        name = record["name"]
        if not record.get("modified"):
            raise PurchaseOrderOutcomeError(f"ERPNext did not provide the saved draft version for {name}")
        try:
            self.client.submit(
                "Purchase Order", name, timeout=_PO_REQUEST_TIMEOUT, allow_redirects=False,
                expected_modified=record["modified"],
            )
        except Exception as exc:
            # A lost response or a competing submit can leave the original PO submitted.
            logger.warning("Keyed PO %s submit needs confirmation: %s", name, type(exc).__name__)
        confirmed = self._read_keyed_purchase_order(data.idempotency_key, name)
        if confirmed["docstatus"] != 1:
            raise PurchaseOrderOutcomeError(f"ERPNext PO {name} submission could not be confirmed")
        self._check_persisted_po(expected, confirmed)
        return self._map_purchase_order(confirmed)

    def _update_mr_ordered_status(self, mr_id: str):
        """Update Material Request to reflect it has been ordered.

        ERPNext auto-calculates status from per_ordered on items. We need to
        set ordered_qty on each MR item to match qty, then save to trigger
        the status recalculation to "Ordered".
        """
        base = self.client.base_url
        session = self.client.session

        # Get the MR with items to find item row names
        try:
            mr = self.client.get("Material Request", mr_id)
        except Exception as e:
            logger.warning(f"Could not fetch MR {mr_id}: {e}")
            return

        # Update each item's ordered_qty to match qty (marks as fully ordered)
        items_update = []
        for item in mr.get("items", []):
            items_update.append({
                "name": item.get("name"),
                "ordered_qty": item.get("qty", 0),
            })

        try:
            resp = session.put(
                f"{base}/api/resource/Material Request/{mr_id}",
                json={"items": items_update, "per_ordered": 100},
            )
            if resp.ok:
                logger.info(f"Updated MR {mr_id} items ordered_qty — status should recalculate to Ordered")
            else:
                logger.warning(f"PUT update for MR {mr_id} returned {resp.status_code}: {resp.text[:300]}")
        except Exception as e:
            logger.warning(f"PUT update failed for MR {mr_id}: {e}")

        # Fallback: use frappe.db.set_value to force status (bypasses validation)
        try:
            resp = session.post(
                f"{base}/api/method/frappe.client.set_value",
                json={
                    "doctype": "Material Request",
                    "name": mr_id,
                    "fieldname": "per_ordered",
                    "value": 100,
                },
            )
            logger.info(f"Set per_ordered=100 for MR {mr_id}: {resp.status_code}")
        except Exception as e:
            logger.warning(f"set_value per_ordered failed for MR {mr_id}: {e}")

    # --- Receipts (Purchase Receipts) ---

    def list_receipts(self, order_id: Optional[str] = None) -> ReceiptList:
        filters: list = [["docstatus", "!=", 2]]  # exclude cancelled
        if order_id:
            # Frappe child table filter: ["ChildDoctype", "field", "=", "value"]
            filters.append(["Purchase Receipt Item", "purchase_order", "=", order_id])
        records = self.client.get_list("Purchase Receipt", fields=RECEIPT_LIST_FIELDS,
                                       filters=filters or None,
                                       order_by="posting_date desc")
        # Deduplicate: child table filters return one row per matching item
        seen = set()
        unique_records = []
        for r in records:
            if r["name"] not in seen:
                seen.add(r["name"])
                unique_records.append(r)
        records = unique_records
        # Enrich with PO reference and item count from detail (PO ref is on items, not header)
        receipts = []
        for r in records:
            mapped = map_record(r, RECEIPT_TO_CANONICAL)
            try:
                detail = self.client.get("Purchase Receipt", r["name"])
                items = detail.get("items", [])
                mapped["line_items"] = [ReceiptLineItem(**map_record(item, RECEIPT_ITEM_TO_CANONICAL)) for item in items]
                # Set order_id from first item's purchase_order
                if items and not mapped.get("order_id"):
                    mapped["order_id"] = items[0].get("purchase_order", "")
            except Exception:
                mapped["line_items"] = []
            receipts.append(Receipt(**mapped))
        return ReceiptList(receipts=receipts, total_count=len(receipts))

    def get_receipt(self, receipt_id: str) -> Receipt:
        record = self.client.get("Purchase Receipt", receipt_id)
        mapped = map_record(record, RECEIPT_TO_CANONICAL)
        items = []
        for item in record.get("items", []):
            line = map_record(item, RECEIPT_ITEM_TO_CANONICAL)
            items.append(ReceiptLineItem(**line))
        mapped["line_items"] = items
        return Receipt(**mapped)

    def create_receipt(self, data: ReceiptCreate) -> Receipt:
        # Fetch PO items to get ERPNext row names and rates
        # Required for ERPNext to auto-update PO received quantities
        po_item_map: dict[str, dict] = {}  # item_code → {name, rate}
        if data.order_id:
            try:
                po_doc = self.client.get("Purchase Order", data.order_id)
                for po_item in po_doc.get("items", []):
                    po_item_map[po_item.get("item_code", "")] = {
                        "name": po_item.get("name", ""),
                        "rate": po_item.get("rate", 0),
                    }
            except Exception as e:
                logger.warning("Failed to fetch PO %s for item linkage: %s", data.order_id, e)

        items = []
        for li in data.line_items:
            item = {
                "item_code": li.item_id,
                "qty": li.quantity_received,
                "warehouse": DEFAULT_WAREHOUSE,
                "purchase_order": data.order_id,
            }
            # Link to specific PO item row and set rate for proper tracking
            po_info = po_item_map.get(li.item_id, {})
            if po_info.get("name"):
                item["purchase_order_item"] = po_info["name"]
            if po_info.get("rate"):
                item["rate"] = po_info["rate"]
            if li.rejected_quantity:
                item["rejected_qty"] = li.rejected_quantity
            items.append(item)

        doc = self.client.insert("Purchase Receipt", {
            "supplier": data.supplier_id,
            "company": DEFAULT_COMPANY,
            "items": items,
        })
        if doc and doc.get("name"):
            self.client.submit("Purchase Receipt", doc["name"])
            return self.get_receipt(doc["name"])
        raise ValueError("Failed to create receipt in ERPNext")

    # --- Invoices (Purchase Invoices) ---

    def list_invoices(self, supplier_id: Optional[str] = None,
                      status: Optional[str] = None) -> InvoiceList:
        filters: list = [["docstatus", "!=", 2]]  # exclude cancelled
        if supplier_id:
            filters.append(["supplier", "=", supplier_id])
        if status:
            filters.append(["status", "=", status.replace("_", " ").title()])
        records = self.client.get_list("Purchase Invoice", fields=INVOICE_LIST_FIELDS,
                                       filters=filters or None,
                                       order_by="posting_date desc")
        # Enrich with PO reference and items (PO ref is on items, not header)
        invoices = []
        for r in records:
            mapped = map_record(r, INVOICE_TO_CANONICAL)
            mapped["status"] = map_status_to_canonical(r.get("status", ""), "Purchase Invoice")
            try:
                detail = self.client.get("Purchase Invoice", r["name"])
                items = detail.get("items", [])
                mapped["line_items"] = [InvoiceLineItem(**map_record(item, INVOICE_ITEM_TO_CANONICAL)) for item in items]
                if items and not mapped.get("order_id"):
                    mapped["order_id"] = items[0].get("purchase_order", "")
            except Exception:
                mapped["line_items"] = []
            invoices.append(Invoice(**mapped))
        return InvoiceList(invoices=invoices, total_count=len(invoices))

    def get_invoice(self, invoice_id: str) -> Invoice:
        record = self.client.get("Purchase Invoice", invoice_id)
        mapped = map_record(record, INVOICE_TO_CANONICAL)
        mapped["status"] = map_status_to_canonical(record.get("status", ""), "Purchase Invoice")
        items = []
        for item in record.get("items", []):
            line = map_record(item, INVOICE_ITEM_TO_CANONICAL)
            items.append(InvoiceLineItem(**line))
        mapped["line_items"] = items
        return Invoice(**mapped)

    def create_invoice(self, data: InvoiceCreate) -> Invoice:
        # Fetch PO items for row name linkage (purchase_order_item)
        # Required for ERPNext to auto-update PO billed quantities
        po_item_map: dict[str, str] = {}  # item_code → PO row name
        po_id = data.order_id or (data.line_items[0].order_id if data.line_items else "")
        if po_id:
            try:
                po_doc = self.client.get("Purchase Order", po_id)
                for po_item in po_doc.get("items", []):
                    po_item_map[po_item.get("item_code", "")] = po_item.get("name", "")
            except Exception as e:
                logger.warning("Failed to fetch PO %s for invoice linkage: %s", po_id, e)

        items = []
        for li in data.line_items:
            item = {
                "item_code": li.item_id,
                "qty": li.quantity,
                "rate": li.unit_price,
            }
            if li.order_id:
                item["purchase_order"] = li.order_id
                # Link to specific PO item row for proper billed qty tracking
                if li.item_id in po_item_map:
                    item["po_detail"] = po_item_map[li.item_id]
            if li.receipt_id:
                item["purchase_receipt"] = li.receipt_id
            items.append(item)

        doc = self.client.insert("Purchase Invoice", {
            "supplier": data.supplier_id,
            "bill_no": data.vendor_invoice_number,
            "bill_date": data.invoice_date,
            "due_date": data.due_date,
            "company": DEFAULT_COMPANY,
            "items": items,
        })
        if doc and doc.get("name"):
            self.client.submit("Purchase Invoice", doc["name"])
            return self.get_invoice(doc["name"])
        raise ValueError("Failed to create invoice in ERPNext")

    # --- Payments ---

    def list_payments(self) -> PaymentList:
        records = self.client.get_list(
            "Payment Entry",
            fields=PAYMENT_LIST_FIELDS,
            filters=[["payment_type", "=", "Pay"]],
            order_by="posting_date desc",
        )
        payments = []
        for r in records:
            mapped = map_record(r, PAYMENT_TO_CANONICAL)
            mapped["status"] = map_status_to_canonical(r.get("status", ""), "Payment Entry")
            payments.append(Payment(**mapped))
        return PaymentList(payments=payments, total_count=len(payments))

    def create_payment(self, data: PaymentCreate) -> Payment:
        """Create AND post a Payment Entry.

        A normal return means the entry is posted (docstatus 1). Every other
        outcome raises, because in ERPNext `insert` only creates a draft and
        `submit` is the step that moves money; a caller handed a draft that
        looks like a payment will retry and pay the supplier twice.

        Raises:
            DuplicatePaymentError: a Payment Entry (draft or posted) already
                references `data.invoice_id`. Nothing was inserted.
            PaymentNotPostedError: the entry was inserted but is not posted.
                `outcome` is "rejected" (ERPNext refused it, still a draft) or
                "unknown" (the submit call failed without saying whether it
                applied, and the re-read could not confirm). Never remedy an
                "unknown" by creating another payment.
            ValueError: ERPNext did not create the draft at all.
        """
        if data.invoice_id:
            self._assert_no_payment_for_invoice(data.invoice_id)

        doc_data = {
            "payment_type": "Pay",
            "party_type": "Supplier",
            "party": data.supplier_id,
            "paid_amount": data.amount,
            "received_amount": data.amount,
            "source_exchange_rate": 1.0,
            "target_exchange_rate": 1.0,
            "paid_from": "Cash - AMG",
            "paid_to": "Creditors - AMG",
            "mode_of_payment": data.mode_of_payment or "Wire Transfer",
            "company": DEFAULT_COMPANY,
        }
        if data.invoice_id:
            # The allocation must be the invoice's real outstanding amount. If
            # that cannot be read, stop: guessing it from the caller-supplied
            # amount changes the write-off the deduction logic computes below.
            try:
                inv = self.client.get("Purchase Invoice", data.invoice_id)
            except Exception as e:
                raise ValueError(
                    f"Cannot read Purchase Invoice {data.invoice_id} to allocate payment: {e}"
                ) from e
            invoice_outstanding = float(
                inv.get("outstanding_amount", 0) or inv.get("grand_total", 0) or data.amount
            )

            # Allocate the full outstanding amount against the invoice
            doc_data["references"] = [{
                "reference_doctype": "Purchase Invoice",
                "reference_name": data.invoice_id,
                "allocated_amount": invoice_outstanding,
            }]

            # If paying less than outstanding (e.g., early payment discount),
            # add deductions to balance the entry.
            # ERPNext Pay type formula: difference = paid - allocated - deductions
            # So deduction must be NEGATIVE to balance: paid - allocated - (-discount) = 0
            if data.deductions:
                doc_data["deductions"] = [
                    {
                        "account": d.account if hasattr(d, 'account') else d.get("account", "Write Off - AMG"),
                        "cost_center": d.cost_center if hasattr(d, 'cost_center') else d.get("cost_center", "Main - AMG"),
                        "amount": -(abs(d.amount if hasattr(d, 'amount') else d.get("amount", 0))),
                    }
                    for d in data.deductions
                ]
            elif data.amount < invoice_outstanding:
                # Fallback: auto-create deduction for the difference
                discount = round(invoice_outstanding - data.amount, 2)
                doc_data["deductions"] = [{
                    "account": "Write Off - AMG",
                    "cost_center": "Main - AMG",
                    "amount": -discount,
                }]

        doc = self.client.insert("Payment Entry", doc_data)
        if not doc or not doc.get("name"):
            raise ValueError("Failed to create payment in ERPNext")
        name = doc["name"]

        # Post the draft. Classify a failure by what it tells us about ERPNext's
        # state: a 4xx is a validation rejection (definitely still a draft);
        # anything else (timeout, dropped connection, 5xx) says nothing about
        # whether the submit applied.
        outcome: Optional[str] = None
        detail = ""
        try:
            self.client.submit("Payment Entry", name)
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            outcome = "rejected" if status is not None and 400 <= status < 500 else "unknown"
            detail = f"submit returned HTTP {status}: {e}"
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            outcome = "unknown"
            detail = f"submit did not complete: {type(e).__name__}: {e}"
        except Exception as e:
            outcome = "unknown"
            detail = f"submit failed: {type(e).__name__}: {e}"

        # Re-read the authoritative state. This resolves most "unknown" cases
        # and guards against a submit that returned success but did not stick.
        try:
            record = self.client.get("Payment Entry", name)
        except Exception as e:
            raise PaymentNotPostedError(
                name, "unknown",
                (detail + "; " if detail else "") + f"could not re-read entry: {e}",
            ) from e

        if int(record.get("docstatus", 0) or 0) != 1:
            if outcome is None:
                # submit() raised nothing, yet the entry is not posted.
                outcome, detail = "unknown", f"submit returned without error but docstatus is {record.get('docstatus')}"
            logger.error("Payment Entry %s not posted (%s): %s", name, outcome, detail)
            raise PaymentNotPostedError(name, outcome, detail)

        if outcome is not None:
            logger.warning("Payment Entry %s posted despite submit error (%s); treating as success", name, detail)

        mapped = map_record(record, PAYMENT_TO_CANONICAL)
        mapped["status"] = map_status_to_canonical(record.get("status", ""), "Payment Entry")
        return Payment(**mapped)

    def _assert_no_payment_for_invoice(self, invoice_id: str) -> None:
        """Idempotency guard: refuse to insert a second Payment Entry for an invoice.

        Checks drafts and posted entries (docstatus 0 and 1; cancelled entries
        are excluded). If the check itself fails we raise rather than proceed,
        because "could not verify" must not become "paid twice".
        """
        try:
            existing = self.client.get_list(
                "Payment Entry",
                fields=["name", "docstatus", "status"],
                # Child-table filter: Payment Entry Reference rows pointing at this invoice.
                filters=[
                    ["Payment Entry Reference", "reference_name", "=", invoice_id],
                    ["Payment Entry Reference", "reference_doctype", "=", "Purchase Invoice"],
                    ["docstatus", "<", 2],
                ],
                limit=5,
            )
        except Exception as e:
            raise ValueError(
                f"Cannot verify existing payments for invoice {invoice_id}; refusing to create one: {e}"
            ) from e

        if not existing:
            return
        # Prefer reporting a posted entry over a draft if both exist.
        existing.sort(key=lambda r: -int(r.get("docstatus", 0) or 0))
        hit = existing[0]
        status = "submitted" if int(hit.get("docstatus", 0) or 0) == 1 else "draft"
        raise DuplicatePaymentError(invoice_id, hit["name"], status)

    # --- Analytics ---

    def get_spend_summary(self) -> SpendSummary:
        total_orders = self.client.get_count("Purchase Order", [["docstatus", "=", 1]])
        total_invoices = self.client.get_count("Purchase Invoice", [["docstatus", "=", 1]])
        total_suppliers = self.client.get_count("Supplier")
        open_orders = self.client.get_count("Purchase Order", [
            ["docstatus", "=", 1],
            ["status", "not in", ["Completed", "Cancelled", "Closed"]],
        ])
        unpaid_invoices = self.client.get_count("Purchase Invoice", [
            ["docstatus", "=", 1],
            ["outstanding_amount", ">", 0],
        ])
        from datetime import date
        today_str = date.today().isoformat()
        overdue_invoices = self.client.get_count("Purchase Invoice", [
            ["docstatus", "=", 1],
            ["outstanding_amount", ">", 0],
            ["due_date", "<", today_str],
        ])

        # Sum total spend from submitted POs
        pos = self.client.get_list("Purchase Order",
                                   fields=["sum(grand_total) as total"],
                                   filters=[["docstatus", "=", 1]])
        total_spend = float(pos[0].get("total", 0) or 0) if pos else 0.0

        return SpendSummary(
            total_spend=total_spend,
            total_orders=total_orders,
            total_invoices=total_invoices,
            total_suppliers=total_suppliers,
            open_orders=open_orders,
            unpaid_invoices=unpaid_invoices,
            overdue_invoices=overdue_invoices,
        )

    def get_supplier_performance(self) -> SupplierPerformanceList:
        # Get PO totals grouped by supplier
        pos = self.client.get_list(
            "Purchase Order",
            fields=["supplier", "supplier_name",
                     "count(name) as order_count",
                     "sum(grand_total) as total_spend"],
            filters=[["docstatus", "=", 1]],
            group_by="supplier",
            order_by="total_spend desc",
        )
        perfs = []
        for r in pos:
            sid = r.get("supplier") or ""
            sname = r.get("supplier_name") or ""
            if not sid:
                continue  # skip records with null supplier
            perfs.append(SupplierPerformance(
                supplier_id=sid,
                supplier_name=sname or sid,
                total_orders=int(r.get("order_count", 0) or 0),
                total_spend=float(r.get("total_spend", 0) or 0),
            ))
        return SupplierPerformanceList(suppliers=perfs, total_count=len(perfs))

    # --- Budgets ---

    def get_budget_status(self, cost_center: Optional[str] = None) -> BudgetStatusList:
        today = date.today()
        fiscal_year, starts, ends = self.client.get_fiscal_year(
            DEFAULT_COMPANY, today.isoformat(), timeout=_PO_REQUEST_TIMEOUT,
        )
        if not date.fromisoformat(starts) <= today <= date.fromisoformat(ends):
            raise ValueError("ERPNext fiscal year does not cover the current date")
        company = self.client.get("Company", DEFAULT_COMPANY, timeout=_PO_REQUEST_TIMEOUT)
        currency = company.get("default_currency")
        if company.get("name") != DEFAULT_COMPANY or not currency:
            raise ValueError("ERPNext company currency is unavailable")

        filters = [
            ["company", "=", DEFAULT_COMPANY], ["fiscal_year", "=", fiscal_year],
            ["budget_against", "=", "Cost Center"], ["docstatus", "=", 1],
        ]
        if cost_center:
            filters.append(["cost_center", "=", cost_center])

        budgets_raw = self.client.get_list(
            "Budget", fields=["name"], filters=filters,
            timeout=_PO_REQUEST_TIMEOUT, require_data=True,
        )

        results = []
        for b in budgets_raw:
            detail = self.client.get("Budget", b["name"], timeout=_PO_REQUEST_TIMEOUT)
            cc = detail.get("cost_center")
            if (
                detail.get("name") != b["name"] or detail.get("company") != DEFAULT_COMPANY
                or detail.get("fiscal_year") != fiscal_year or detail.get("docstatus") != 1
                or detail.get("budget_against") != "Cost Center" or not cc or (cost_center and cc != cost_center)
                or not detail.get("accounts")
            ):
                raise ValueError("ERPNext returned incomplete or inconsistent budget details")
            budget_amount = sum(Decimal(str(acct["budget_amount"])) for acct in detail["accounts"])
            cc_name = cc.split(" - ")[0] if " - " in cc else cc

            # Submitted PO commitments are compared in company currency for this fiscal year.
            pos = self.client.get_list(
                "Purchase Order",
                fields=["sum(base_grand_total) as total"],
                filters=[
                    ["company", "=", DEFAULT_COMPANY], ["docstatus", "=", 1],
                    ["cost_center", "=", cc],
                    ["transaction_date", "between", [starts, ends]],
                ],
                timeout=_PO_REQUEST_TIMEOUT, require_data=True,
            )
            if len(pos) != 1 or "total" not in pos[0]:
                raise ValueError("ERPNext did not provide the total PO commitments")
            total = pos[0]["total"]
            actual_spend = Decimal(str(total)) if total is not None else Decimal(0)
            if not budget_amount.is_finite() or not actual_spend.is_finite() or actual_spend < 0:
                raise ValueError("ERPNext returned invalid budget amounts")

            remaining = budget_amount - actual_spend
            utilization = round((actual_spend / budget_amount) * 100, 1) if budget_amount > 0 else 0

            results.append(BudgetStatus(
                cost_center=cc,
                cost_center_name=cc_name,
                fiscal_year=fiscal_year,
                budget_amount=budget_amount,
                actual_spend=actual_spend,
                remaining=remaining,
                utilization_pct=utilization,
                exceeded=remaining < 0,
                currency=currency,
            ))

        return BudgetStatusList(budgets=results, total_count=len(results))

    # --- Cost Centers ---

    def list_payment_terms(self) -> list[dict]:
        records = self.client.get_list(
            "Payment Terms Template",
            fields=["name"],
            limit=0,
        )
        return [{"name": r["name"]} for r in records]

    def list_cost_centers(self) -> "CostCenterList":
        from adapters.models import CostCenter, CostCenterList
        records = self.client.get_list(
            "Cost Center",
            fields=["name", "cost_center_name", "parent_cost_center", "is_group"],
            filters=[["is_group", "=", 0], ["company", "=", DEFAULT_COMPANY]],
        )
        centers = [CostCenter(
            cost_center_id=r["name"],
            cost_center_name=r.get("cost_center_name", r["name"].split(" - ")[0]),
            parent=r.get("parent_cost_center", ""),
        ) for r in records]
        return CostCenterList(cost_centers=centers, total_count=len(centers))

    # --- File Attachments ---

    def attach_file(self, docname: str, doctype: str, file_bytes: bytes, filename: str) -> dict:
        """Attach a file to an ERPNext document using the Frappe upload_file API."""
        url = f"{self.client.base_url}/api/method/upload_file"
        files = {"file": (filename, io.BytesIO(file_bytes), "application/pdf")}
        data = {
            "doctype": doctype,
            "docname": docname,
            "is_private": 1,
        }
        resp = self.client.session.post(url, files=files, data=data)
        resp.raise_for_status()
        result = resp.json().get("message", {})
        logger.info("Attached %s to %s %s: %s", filename, doctype, docname, result.get("file_url", ""))
        return {"file_url": result.get("file_url", ""), "file_name": result.get("file_name", "")}
