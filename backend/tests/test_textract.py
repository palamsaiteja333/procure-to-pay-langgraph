# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Verify invoice extraction through the Bedrock and Textract adapters."""

import io
import json
from unittest.mock import Mock

import pytest

from services import textract


PDF = b"%PDF-test"
INVOICE = {
    "vendor_name": "Acme Industrial Supply",
    "invoice_number": "INV-42",
    "invoice_date": "2026-03-15",
    "due_date": "2026-04-14",
    "po_number": "PO-15",
    "total_amount": 275.0,
    "line_items": [{"item_code": "BOLT-M10", "quantity": 500, "unit_price": 0.55}],
}


@pytest.fixture
def clients(monkeypatch):
    clients = {service: Mock() for service in ("bedrock-runtime", "textract", "s3")}
    monkeypatch.setattr(textract.boto3, "client", lambda service, **kwargs: clients[service])
    return clients


def test_bedrock_extracts_fields_and_line_items(clients):
    bedrock = clients["bedrock-runtime"]
    bedrock.converse.return_value = {"output": {"message": {"content": [{"text": json.dumps(INVOICE)}]}}}

    result = textract.extract_invoice_from_bytes(PDF)

    assert result == {**INVOICE, "confidence": textract._assess_confidence(INVOICE)}
    assert result["confidence"]["tier"] == "AUTO_ACCEPT"
    kwargs = bedrock.converse.call_args.kwargs
    assert kwargs["messages"][0]["content"][0]["document"]["source"] == {"bytes": PDF}
    assert kwargs["outputConfig"]["textFormat"]["type"] == "json_schema"
    clients["textract"].analyze_expense.assert_not_called()


def test_falls_back_to_textract_and_routes_header_only_for_review(clients):
    clients["bedrock-runtime"].converse.side_effect = TimeoutError("Bedrock did not respond")
    fields = {
        "VENDOR_NAME": "Acme Industrial Supply", "INVOICE_RECEIPT_ID": "INV-42",
        "INVOICE_RECEIPT_DATE": "2026-03-15", "DUE_DATE": "2026-04-14",
        "PO_NUMBER": "PO-15", "TOTAL": "$12,345.67", "SUBTOTAL": "$11,223.34",
        "TAX": "$1,122.33", "PAYMENT_TERMS": "Net 30",
    }
    clients["textract"].analyze_expense.return_value = {"ExpenseDocuments": [{"SummaryFields": [
        {"Type": {"Text": name}, "ValueDetection": {"Text": value}}
        for name, value in fields.items()
    ]}]}

    result = textract.extract_invoice_from_bytes(PDF)

    clients["textract"].analyze_expense.assert_called_once_with(Document={"Bytes": PDF})
    assert (result["vendor_name"], result["invoice_number"], result["po_number"]) == (
        "Acme Industrial Supply", "INV-42", "PO-15",
    )
    assert result["total_amount"] == 12345.67
    assert result["subtotal"] == 11223.34
    assert result["tax_amount"] == 1122.33
    assert result["payment_terms"] == "Net 30"
    assert result["line_items"] == []
    assert result["confidence"]["tier"] == "REVIEW"


def test_fallback_without_fields_requires_manual_extraction(clients):
    clients["bedrock-runtime"].converse.return_value = {"output": {}}
    clients["textract"].analyze_expense.return_value = {"ExpenseDocuments": []}

    result = textract.extract_invoice_from_bytes(PDF)

    assert result["vendor_name"] is None
    assert result["total_amount"] is None
    assert result["line_items"] == []
    assert result["confidence"]["tier"] == "MANUAL"
    assert set(result["confidence"]["missing_fields"]) == {
        "vendor_name", "invoice_number", "total_amount", "invoice_date", "po_number",
    }


def test_reads_s3_document_through_the_same_extraction_path(clients):
    clients["s3"].get_object.return_value = {"Body": io.BytesIO(PDF)}
    clients["bedrock-runtime"].converse.return_value = {
        "output": {"message": {"content": [{"text": json.dumps(INVOICE)}]}},
    }

    assert textract.extract_invoice_from_s3("invoices", "example.pdf")["invoice_number"] == "INV-42"
    clients["s3"].get_object.assert_called_once_with(Bucket="invoices", Key="example.pdf")


@pytest.mark.parametrize(("amount", "expected"), [
    ("$1,234.56", 1234.56), ("1234.56", 1234.56), ("USD 1234.56", 1234.56),
    ("", 0), ("N/A", 0),
])
def test_parse_amount(amount, expected):
    assert textract._parse_amount(amount) == expected
