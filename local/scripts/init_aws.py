# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Provision the local emulators to match the deployed AWS resources.

Creates tables in the persistent DynamoDB Local service, plus the S3 document
bucket and ERPNext credentials secret in Moto. The supervisor can rerun this
on every start without deleting existing records.

Table schema mirrors infra/lib/p2p-agentic-stack.ts (prefix ``p2p-dev``):
  <prefix>-chat-sessions       PK user_id
  <prefix>-document-lifecycle  PK document_id
  <prefix>-invoice-jobs        PK job_id
  <prefix>-simulation-state    PK see CDK
  <prefix>-agent-jobs          PK job_id     (referenced by services code)
  <prefix>-agent-errors        PK error_id   (referenced by services code)
  <prefix>-workflow-checkpoints  PK PK, SK SK (LangGraph)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, ConnectionClosedError, ConnectionError, ReadTimeoutError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "config"))
import harness_env as H  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s init-aws %(message)s")
logger = logging.getLogger("init_aws")

# (logical name, partition-key attribute). Prefix is applied at creation.
_TABLES = [
    ("chat-sessions", "user_id"),
    ("document-lifecycle", "document_id"),
    ("invoice-jobs", "job_id"),
    ("agent-jobs", "job_id"),
    ("agent-errors", "error_id"),
    ("simulation-state", "pk"),
]


def _client(service: str):
    # Provisioning uses fixed loopback endpoints and does not need AWS credentials.
    endpoint_key = {
        "dynamodb": "AWS_ENDPOINT_URL_DYNAMODB",
        "s3": "AWS_ENDPOINT_URL_S3",
        "secretsmanager": "AWS_ENDPOINT_URL_SECRETS_MANAGER",
    }[service]
    return boto3.client(
        service,
        region_name=H.REGION,
        aws_access_key_id="local",
        aws_secret_access_key="local",
        endpoint_url=H.endpoint_env()[endpoint_key],
        config=Config(connect_timeout=2, read_timeout=2, proxies={}, retries={"total_max_attempts": 1}),
    )


def _wait_for_dynamodb(ddb, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            ddb.list_tables(Limit=1)
            return
        except (ConnectionError, ConnectionClosedError, ReadTimeoutError) as exc:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "DynamoDB Local did not become ready. Start it with make -C local dynamodb."
                ) from exc
            time.sleep(0.5)  # nosemgrep: arbitrary-sleep -- DynamoDB Local readiness poll


def _create_tables() -> None:
    ddb = _client("dynamodb")
    _wait_for_dynamodb(ddb)
    tables = {f"{H.TABLE_PREFIX}-{name}": (pk,) for name, pk in _TABLES}
    tables[H.CHECKPOINT_TABLE] = ("PK", "SK")
    for table_name, keys in tables.items():
        key_schema = [{"AttributeName": name, "KeyType": role} for name, role in zip(keys, ("HASH", "RANGE"))]
        attributes = [{"AttributeName": name, "AttributeType": "S"} for name in keys]
        try:
            ddb.create_table(
                TableName=table_name,
                KeySchema=key_schema,
                AttributeDefinitions=attributes,
                BillingMode="PAY_PER_REQUEST",
            )
            logger.info("created table %s", table_name)
        except ddb.exceptions.ResourceInUseException:
            existing = ddb.describe_table(TableName=table_name)["Table"]
            if existing["KeySchema"] != key_schema or any(
                attr not in existing["AttributeDefinitions"] for attr in attributes
            ):
                raise RuntimeError(f"DynamoDB Local table {table_name} has a different key schema") from None
            logger.info("table %s already exists", table_name)


def _create_bucket() -> None:
    s3 = _client("s3")
    bucket_args = {"Bucket": H.S3_BUCKET}
    if H.REGION != "us-east-1":
        bucket_args["CreateBucketConfiguration"] = {"LocationConstraint": H.REGION}
    try:
        s3.create_bucket(**bucket_args)
        logger.info("created bucket %s", H.S3_BUCKET)
    except ClientError as e:
        if e.response["Error"]["Code"] in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            logger.info("bucket %s already exists", H.S3_BUCKET)
        else:
            raise


def _create_secret() -> None:
    """Seed the ERPNext credentials secret the backend bootstraps from.

    canonical_api._bootstrap_from_secret() reads ERPNEXT_SECRET_ARN; for local
    we store service creds so the adapter can talk to the local ERPNext. Values
    come from the env (populated from local/.env) with empty defaults.
    """
    sm = _client("secretsmanager")
    payload = {
        "service_api_key": os.environ.get("ERPNEXT_API_KEY", ""),
        "service_api_secret": os.environ.get("ERPNEXT_API_SECRET", ""),
        "admin_username": os.environ.get("ERPNEXT_USER", "Administrator"),
        "admin_password": os.environ.get("ERPNEXT_PASSWORD", ""),
    }
    try:
        sm.create_secret(Name=H.ERPNEXT_SECRET_NAME, SecretString=json.dumps(payload))
        # Logs the secret's NAME, not its contents.
        logger.info("created secret store %s", H.ERPNEXT_SECRET_NAME)  # nosemgrep: python-logger-credential-disclosure
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceExistsException":
            sm.put_secret_value(SecretId=H.ERPNEXT_SECRET_NAME, SecretString=json.dumps(payload))
            # Logs the secret's NAME, not its contents.
            logger.info("updated secret store %s", H.ERPNEXT_SECRET_NAME)  # nosemgrep: python-logger-credential-disclosure
        else:
            raise


def main() -> int:
    _create_tables()
    _create_bucket()
    _create_secret()
    logger.info("local AWS provisioning complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
