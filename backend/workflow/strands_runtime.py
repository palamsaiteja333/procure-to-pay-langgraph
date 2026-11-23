# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Use the existing Strands agents and ERP Gateway from the graph."""

import asyncio
import json
import time
from datetime import timedelta
from uuid import uuid4

from strands import Agent
from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookProvider, HookRegistry
from strands.tools.executors import SequentialToolExecutor

from agents.p2p_workflow import _extract_text, _parse_json
from utils import workflow_telemetry as telemetry
from utils.progress_hooks import ProgressBridge

CREATE_PO = "erp___create_purchase_order"
RECONCILE_PO = "erp___reconcile_purchase_order"


def _tool_payload(result: dict) -> dict:
    def unwrap(payload: dict) -> dict:
        value = payload.get("result")
        return value if len(payload) == 1 and isinstance(value, dict) else payload

    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return unwrap(structured)
    for item in result.get("content", []):
        if isinstance(item.get("json"), dict):
            return unwrap(item["json"])
        if "text" in item:
            parsed = _parse_json(item["text"])
            if isinstance(parsed, dict):
                return unwrap(parsed)
    return {}


class _WorkflowToolGuard(HookProvider):
    def __init__(self, before_po):
        self.before_po = before_po
        self.unconfirmed_call = None

    def register_hooks(self, registry: HookRegistry, **kwargs):
        registry.add_callback(BeforeToolCallEvent, self.before_tool)
        registry.add_callback(AfterToolCallEvent, self.after_tool)

    def before_tool(self, event: BeforeToolCallEvent):
        name = event.tool_use.get("name")
        if name == RECONCILE_PO:
            event.cancel_tool = "Reconciliation is handled by the workflow."
        if name != CREATE_PO or event.cancel_tool:
            return
        if self.before_po is None:
            event.cancel_tool = "A purchase order requires a workflow approval."
        elif self.unconfirmed_call is not None:
            event.cancel_tool = "An earlier PO write is unconfirmed. The workflow will reconcile it."
        else:
            error = self.before_po(event.tool_use.get("input", {}))
            if error:
                event.cancel_tool = error
            else:
                self.unconfirmed_call = event.tool_use["toolUseId"]

    def after_tool(self, event: AfterToolCallEvent):
        if (
            self.unconfirmed_call is not None
            and event.tool_use.get("toolUseId") == self.unconfirmed_call
            and isinstance(event.result, dict)
            and event.result.get("status") == "success"
            and _tool_payload(event.result).get("order_id")
        ):
            self.unconfirmed_call = None


class StrandsWorkflowRuntime:
    def __init__(self, mcp_factory, model_factory, system_prompt, local_tools):
        self.mcp_factory = mcp_factory
        self.model_factory = model_factory
        self.system_prompt = system_prompt
        self.local_tools = local_tools

    def _client(self):
        client = self.mcp_factory()
        if client is None:
            raise RuntimeError("MCP Gateway is unavailable")
        return client

    async def call_tool(self, name: str, arguments: dict) -> dict:
        def call():
            with self._client() as client:
                result = client.call_tool_sync(
                    str(uuid4()), name, arguments, read_timeout_seconds=timedelta(seconds=35),
                )
            parsed = _tool_payload(result)
            if not parsed:
                raise RuntimeError(f"The ERP tool {name} returned no usable result")
            return parsed

        started = time.perf_counter()
        metrics = {}
        with telemetry.span(f"workflow.tool.{name}"):
            try:
                return await asyncio.to_thread(call)
            except Exception:
                metrics["ERPToolFailures"] = 1
                raise
            finally:
                metrics["ERPToolDurationMs"] = round((time.perf_counter() - started) * 1000)
                telemetry.record("workflow_tool", metrics=metrics, tool=name)

    async def run_agent(self, agent: str, prompt: str, emit, before_po=None) -> dict:
        bridge = ProgressBridge(agent)
        instance = Agent(
            model=self.model_factory(),
            tools=[self._client(), *self.local_tools(agent)],
            system_prompt=self.system_prompt(agent),
            tool_executor=SequentialToolExecutor() if before_po else None,
            hooks=[bridge.hook_provider, _WorkflowToolGuard(before_po)],
        )
        async for event in bridge.run(instance, prompt):
            emit(json.loads(event))
        if agent_metrics := getattr(bridge.result, "metrics", None):
            usage = agent_metrics.accumulated_usage
            metrics = {
                "BedrockInputTokens": usage["inputTokens"],
                "BedrockOutputTokens": usage["outputTokens"],
            }
            tool_failures = sum(
                metrics.error_count for name, metrics in agent_metrics.tool_metrics.items() if name.startswith("erp___")
            )
            if tool_failures:
                metrics["ERPToolFailures"] = tool_failures
            telemetry.record("workflow_agent_usage", metrics=metrics, step=agent)
        response = _extract_text(bridge.result)
        parsed = _parse_json(response)
        return parsed if isinstance(parsed, dict) else {"raw_response": str(response)}
