# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared requisition-version check for the workflow and the ERP write boundary."""

import hashlib
import json
from typing import Any


def request_version(requisition: dict[str, Any]) -> str:
    """Hash the reviewed business fields independently of ERP fulfillment status."""
    fields = {key: value for key, value in requisition.items() if key != "status"}
    packed = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(packed.encode()).hexdigest()
