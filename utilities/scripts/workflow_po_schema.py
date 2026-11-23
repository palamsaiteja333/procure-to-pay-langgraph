# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Install the unique workflow PO key on an existing ERPNext site."""


def ensure_workflow_po_key(client):
    fieldname = "custom_p2p_order_key"
    name = f"Purchase Order-{fieldname}"
    if not client.exists("Custom Field", name):
        client.insert("Custom Field", {
            "dt": "Purchase Order",
            "fieldname": fieldname,
            "label": "Workflow Purchase Order Key",
            "fieldtype": "Data",
            "insert_after": "supplier",
            "unique": 1,
            "hidden": 1,
            "no_copy": 1,
        })

    field = client.get("Custom Field", name)
    if field.get("fieldtype") != "Data" or not field.get("unique") or field.get("reqd") or field.get("default"):
        raise RuntimeError(f"ERPNext field {name} must be unique and optional, with no default")


if __name__ == "__main__":
    from erpnext_client import ERPNextClient

    ensure_workflow_po_key(ERPNextClient())
    print("Verified the ERPNext workflow purchase order key field")
