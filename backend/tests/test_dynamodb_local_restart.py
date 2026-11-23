# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Verify graph and approval persistence across DynamoDB Local container replacement."""

import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ConnectionClosedError, EndpointConnectionError, ReadTimeoutError

from config import settings
from services import dynamo, lifecycle

from . import test_workflow as workflow_test

pytestmark = pytest.mark.skipif(
    os.environ.get("P2P_TEST_DYNAMODB_LOCAL") != "1",
    reason="Set P2P_TEST_DYNAMODB_LOCAL=1 to use an isolated local Docker database",
)


def docker_command(*args, check=True):
    docker = shutil.which("docker") or "/Applications/Docker.app/Contents/Resources/bin/docker"
    if not Path(docker).is_file():
        pytest.fail("Docker CLI is required for the DynamoDB Local restart test.")
    completed = subprocess.run(
        [docker, *args], capture_output=True, text=True, check=False, timeout=180,
    )
    if check and completed.returncode:
        pytest.fail(f"Docker {args[0]} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


@contextmanager
def dynamodb_container(name, volume):
    docker_command(
        "run", "--detach", "--name", name, "--publish", "127.0.0.1::8000", "--user", "0:0",
        "--mount", f"type=volume,source={volume},target=/home/dynamodblocal/data",
        "--workdir", "/home/dynamodblocal", "amazon/dynamodb-local:latest",
        "-jar", "DynamoDBLocal.jar", "-sharedDb", "-dbPath", "./data", "-disableTelemetry",
    )
    try:
        binding = docker_command("port", name, "8000/tcp")
        port = int(binding.removeprefix("127.0.0.1:"))
        endpoint = f"http://127.0.0.1:{port}"
        resource = boto3.resource(
            "dynamodb", endpoint_url=endpoint, region_name=settings.aws_region,
            aws_access_key_id="localtest", aws_secret_access_key="localtestonly",
            config=Config(connect_timeout=1, read_timeout=1, retries={"total_max_attempts": 1}),
        )
        client = resource.meta.client
        try:
            deadline = time.monotonic() + 25
            while True:
                try:
                    client.list_tables(Limit=1)
                    break
                except (ConnectionClosedError, EndpointConnectionError, ReadTimeoutError):
                    if time.monotonic() >= deadline:
                        pytest.fail(f"DynamoDB Local did not start: {docker_command('logs', name, check=False)}")
                    time.sleep(0.2)  # nosemgrep: arbitrary-sleep -- bounded local service readiness check
            yield endpoint, resource
        finally:
            client.close()
    finally:
        docker_command("stop", name, check=False)
        docker_command("rm", "--force", name)


def test_approval_and_committed_po_survive_database_restarts(tmp_path, monkeypatch):
    # Every test owns its volume, containers, published port, and tables.
    prefix = f"p2p-restart-{uuid4().hex}"
    volume = f"{prefix}-data"
    checkpoint_table = f"{prefix}-workflow-checkpoints"
    erp_path = tmp_path / "test-erp.sqlite"
    document_id = workflow_test.DOCUMENT_ID
    erp = workflow_test.TestERP(erp_path)

    monkeypatch.setenv("DYNAMODB_TABLE_PREFIX", prefix)
    monkeypatch.setenv("P2P_CHECKPOINT_TABLE", checkpoint_table)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "localtest")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "localtestonly")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("AWS_SECURITY_TOKEN", raising=False)
    monkeypatch.setenv("PYTHONPATH", str(workflow_test.BACKEND))
    monkeypatch.setattr(settings, "dynamodb_table_prefix", prefix)

    def configure_endpoint(endpoint, resource):
        monkeypatch.setenv("AWS_ENDPOINT_URL_DYNAMODB", endpoint)
        monkeypatch.setattr(dynamo, "dynamodb", resource)

    def worker(resume_from="", scenario="human", expected_exit=0):
        completed = subprocess.run(
            [sys.executable, workflow_test.__file__, str(erp_path), resume_from, scenario],
            cwd=workflow_test.BACKEND, capture_output=True, text=True, timeout=30,
        )
        assert completed.returncode == expected_exit, completed.stderr
        return [] if expected_exit else json.loads(completed.stdout)

    docker_command("volume", "create", volume)
    try:
        with dynamodb_container(f"{prefix}-initial", volume) as (endpoint, resource):
            configure_endpoint(endpoint, resource)
            workflow_test.create_lifecycle_table(resource)
            workflow_test.create_checkpoint_table(resource, checkpoint_table)
            pending = workflow_test.check_pending(worker())
            assert workflow_test.approve(pending).status_code == 200
            assert erp.calls() == ["sourcing", "requisition"]
            assert erp.order_ids() == []

        with dynamodb_container(f"{prefix}-approved", volume) as (endpoint, resource):
            configure_endpoint(endpoint, resource)
            approved = lifecycle.get_workflow(document_id)
            assert approved["run_id"] == pending["workflow_run_id"]
            assert approved["request_version"] == pending["request_version"]
            assert approved["decision"]["action"] == "HUMAN_APPROVED"
            worker("po_generation", "crash", expected_exit=86)
            assert lifecycle.get_workflow(document_id)["status"] == "PO_GENERATION"
            assert erp.order_ids() == ["PO-0001"]

        with dynamodb_container(f"{prefix}-recovery", volume) as (endpoint, resource):
            configure_endpoint(endpoint, resource)
            # Simulate lease expiration so recovery can run without waiting for the production lease duration.
            resource.Table(f"{prefix}-document-lifecycle").update_item(
                Key={"document_id": document_id}, UpdateExpression="SET workflow_lease_until = :expired",
                ExpressionAttributeValues={":expired": 1},
            )
            for _ in range(2):
                workflow_test.check_complete(worker("po_generation"), ["PO-0001"])
            assert lifecycle.get_workflow(document_id)["run_id"] == pending["workflow_run_id"]
            assert erp.calls() == ["sourcing", "requisition", "po_management"]
            assert erp.order_ids() == ["PO-0001"]
    finally:
        docker_command("volume", "rm", volume)
