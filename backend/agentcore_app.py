# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
AgentCore Runtime entrypoint for P2P agents.

Streams real-time progress events as the agent works using async generator.
The AGENT_NAME env var determines which agent runs.

Architecture:
  - ERP data access: MCP tools via AgentCore Gateway (canonical P2P API)
  - Local computation: budget check, contract lookup, risk scoring
  - Memory: AgentCore Memory for cross-session context
"""

import os
import json
import logging
import time
import uuid

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel

logging.basicConfig(level=logging.INFO, format="%(name)s — %(message)s")
logger = logging.getLogger("p2p.agentcore")

app = BedrockAgentCoreApp()

AGENT_NAME = os.environ.get("AGENT_NAME", "requisition")
GATEWAY_ENDPOINT = os.environ.get("GATEWAY_ENDPOINT", "")
GUARDRAIL_ID = os.environ.get("BEDROCK_GUARDRAIL_ID", "")
GUARDRAIL_VERSION = os.environ.get("BEDROCK_GUARDRAIL_VERSION", "DRAFT")

# Tool descriptions are in utils/progress_hooks.py (shared with ProgressBridge)
from utils.progress_hooks import TOOL_DESCRIPTIONS

AGENT_LABELS = {
    "requisition": "Requisition Agent",
    "sourcing": "Sourcing Agent",
    "po_management": "PO Management Agent",
    "receiving": "Receiving Agent",
    "invoice_matching": "Invoice Matching Agent",
    "payment": "Payment Agent",
}


def _build_mcp_client():
    """Build MCP client for AgentCore Gateway tools (ERP data access)."""
    from utils.mcp_client import build_mcp_client
    return build_mcp_client(GATEWAY_ENDPOINT)


def _build_local_tools(agent_name: str) -> list:
    """Build local computation tools (not ERP data — budget, contracts, scoring)."""
    tools = []

    # Budget check is now via MCP Gateway (erp__get_budget_status) — no local tool needed

    if agent_name in ("sourcing",):
        try:
            from agents.sourcing_agent import build_contract_tools
            tools.extend(build_contract_tools())
        except Exception:
            logger.warning("Sourcing contract tools unavailable")

    if agent_name in ("po_management",):
        try:
            from agents.sourcing_agent import build_contract_tools as src_contract_tools
            tools.extend(src_contract_tools())
        except Exception:
            logger.warning("Sourcing contract tools unavailable for PO management")
        try:
            from agents.po_management_agent import build_contract_tools as po_contract_tools
            tools.extend(po_contract_tools())
        except Exception:
            logger.warning("PO contract tools unavailable")

    return tools


# Agents that get Code Interpreter for computational analysis
CODE_INTERPRETER_AGENTS = {"payment", "sourcing", "invoice_matching"}


def _build_code_interpreter(agent_name: str):
    """Build Code Interpreter tool for agents that need computational capabilities.

    Uses the AgentCore CodeInterpreter resource via the Strands tools package.
    Enables precise financial math, scorecard calculations, and variance analysis.
    """
    if agent_name not in CODE_INTERPRETER_AGENTS:
        return None

    try:
        from strands_tools.code_interpreter import AgentCoreCodeInterpreter

        region = os.environ.get("AWS_REGION_NAME", "us-east-1")
        ci = AgentCoreCodeInterpreter(region=region)
        logger.info(f"[{agent_name}] Code Interpreter tool enabled")
        return ci.code_interpreter
    except Exception as e:
        logger.warning(f"[{agent_name}] Code Interpreter unavailable: {e}")
        return None


def _get_system_prompt(agent_name: str) -> str:
    """Get system prompt for an agent. Builds dynamic prompts if needed."""
    if agent_name == "requisition":
        from agents.requisition_agent import SYSTEM_PROMPT
        return SYSTEM_PROMPT
    elif agent_name == "sourcing":
        from agents.sourcing_agent import SYSTEM_PROMPT, _build_system_prompt
        return SYSTEM_PROMPT or _build_system_prompt()
    elif agent_name == "po_management":
        from agents.po_management_agent import SYSTEM_PROMPT
        return SYSTEM_PROMPT
    elif agent_name == "receiving":
        from agents.receiving_agent import SYSTEM_PROMPT
        return SYSTEM_PROMPT
    elif agent_name == "invoice_matching":
        from agents.invoice_matching_agent import SYSTEM_PROMPT, _build_system_prompt
        return SYSTEM_PROMPT or _build_system_prompt()
    elif agent_name == "payment":
        from agents.payment_agent import SYSTEM_PROMPT
        return SYSTEM_PROMPT
    return ""


def _get_prompt(agent_name: str, document_id: str, user_email: str = "") -> str:
    # Identity clause — ensures every tool call includes user_email for per-user ERP auth
    identity = f' Pass user_email="{user_email}" in every ERP tool call.' if user_email else ""
    prompts = {
        "requisition": f"Analyze purchase requisition requisition_id={document_id}.{identity} Use the tools to retrieve the requisition details, check items, suppliers, look for duplicates, and compare historical pricing. Then provide your recommendation as JSON.",
        "invoice_matching": f"Perform a three-way match on invoice invoice_id={document_id}.{identity} Retrieve the invoice, the purchase order, and any goods receipts. Compare each line item across all three documents. Return your match result as JSON.",
        "sourcing": f"Evaluate vendors for purchase requisition requisition_id={document_id}.{identity} Analyze all suppliers using historical purchase order and receipt data. Recommend the best supplier with scoring and justification.",
        "po_management": f"Generate a purchase order for requisition requisition_id={document_id}.{identity} Retrieve the requisition, validate items, check for consolidation opportunities.",
        "receiving": f"Validate goods receipts for purchase order order_id={document_id}.{identity} Check quantities, delivery timing, and flag any issues.",
        "payment": f"Analyze payment scheduling for invoice invoice_id={document_id}.{identity} Determine optimal payment timing considering discount opportunities.",
    }
    return prompts.get(agent_name, f"Process document {document_id}")


def _validate_with_guardrail(response_text: str) -> dict:
    """Validate agent reasoning against procurement policy guardrail (Automated Reasoning).

    Returns:
        dict with keys:
            valid (bool): True if no policy violations found
            action (str): "NONE" or "GUARDRAIL_INTERVENED"
            findings (list): AR findings with result/rule/explanation
    """
    if not GUARDRAIL_ID:
        return {"valid": True, "action": "NONE", "findings": []}

    try:
        import boto3
        bedrock = boto3.client(
            "bedrock-runtime",
            region_name=os.environ.get("AWS_REGION_NAME", "us-east-1"),
        )
        response = bedrock.apply_guardrail(
            guardrailIdentifier=GUARDRAIL_ID,
            guardrailVersion=GUARDRAIL_VERSION,
            source="OUTPUT",
            content=[{"text": {"text": response_text}}],
        )

        action = response.get("action", "NONE")
        ar_findings = []
        for assessment in response.get("assessments", []):
            ar = assessment.get("automatedReasoningPolicy", {})
            for finding in ar.get("findings", []):
                ar_findings.append({
                    "result": finding.get("result"),
                    "rule": finding.get("rule", ""),
                    "explanation": finding.get("explanation", ""),
                })

        return {
            "valid": action == "NONE",
            "action": action,
            "findings": ar_findings,
        }
    except Exception as e:
        logger.warning("Guardrail validation failed: %s", e)
        return {"valid": True, "action": "ERROR", "findings": [], "error": str(e)}


AGENT_DOCUMENT_TYPES = {
    "requisition": "PR",
    "invoice_matching": "INVOICE",
    "payment": "PAYMENT",
    "sourcing": "SOURCING",
    "po_management": "PO",
    "receiving": "RECEIPT",
}


def _post_process(agent_name: str, parsed: dict) -> dict:
    """Apply deterministic business rules as a second line of defense.

    Cedar Policy handles authorization, Automated Reasoning validates
    against procurement rules. This adds lightweight deterministic checks.
    """
    if agent_name == "requisition":
        total = float(parsed.get("total_amount", 0) or 0)
        risk = parsed.get("risk_level", "")
        recommendation = parsed.get("recommendation", "")

        # Budget FAIL → force HIGH risk + ESCALATE (check first, overrides everything)
        for finding in parsed.get("findings", []):
            if finding.get("check", "").lower().startswith("budget") and finding.get("status") == "FAIL":
                parsed["risk_level"] = "HIGH"
                parsed["auto_approved"] = False
                parsed["recommendation"] = "ESCALATE"
                return parsed

        # HIGH risk or over $50K → always escalate to human
        if risk == "HIGH" or total > 50000:
            parsed["auto_approved"] = False
            if recommendation != "REJECT":
                parsed["recommendation"] = "ESCALATE"
        # LOW/MEDIUM risk, agent recommends APPROVE, under $5K → auto-approve
        elif risk in ("LOW", "MEDIUM") and total <= 5000 and recommendation == "APPROVE":
            parsed["auto_approved"] = True
            parsed["recommendation"] = "APPROVE"
        # Everything else (MEDIUM risk ESCALATE, etc.) → defer to human
        else:
            parsed["auto_approved"] = False
    return parsed


@app.entrypoint
async def invoke(payload):
    """Stream progress events as the agent works, then yield the final result."""
    document_id = payload.get("document_id", "")
    user_email = payload.get("user_email", "")

    # Workflow agent — sourcing, requisition review, approval, then PO.
    if AGENT_NAME == "workflow":
        resume_from = payload.get("resume_from", "")
        async for event in _run_workflow(document_id, user_email, resume_from):
            yield event
        return

    agent_label = AGENT_LABELS.get(AGENT_NAME, AGENT_NAME)
    logger.info(f"[{AGENT_NAME}] Invoking for document: {document_id}, user: {user_email}")
    start_time = time.time()

    # --- Duplicate standalone agent guard ---
    # Resolve tracking doc and check for running entry before starting
    _agent_run_id = ""
    _tracking_doc_id = document_id
    if document_id and AGENT_NAME in ("receiving", "invoice_matching", "payment"):
        from services.lifecycle import is_agent_active, get_lifecycle_by_po, add_run_entry as _add_run, update_run_status as _update_status

        po_id = payload.get("order_id", "")
        if po_id:
            mr_lifecycle = get_lifecycle_by_po(po_id)
            if mr_lifecycle:
                _tracking_doc_id = mr_lifecycle["document_id"]

        active_check = is_agent_active(_tracking_doc_id, AGENT_NAME)
        if active_check["active"] and not active_check["stale"]:
            logger.warning(f"[{AGENT_NAME}] Duplicate blocked for {document_id} (run: {active_check['run_id']})")
            yield json.dumps({
                "type": "error",
                "error": "DUPLICATE_AGENT",
                "message": f"{agent_label} is already running for this document. Please wait for it to complete.",
                "agent": AGENT_NAME,
            }) + "\n"
            return
        elif active_check["active"] and active_check["stale"]:
            old_run_id = active_check.get("run_id", "")
            if old_run_id:
                _update_status(_tracking_doc_id, old_run_id, "failed")
            logger.warning(f"[{AGENT_NAME}] Stale agent run for {document_id} (run={old_run_id}) — cleaned up")

        # Record a "running" entry so future invocations can detect it
        _agent_run_id = _add_run(
            document_id=_tracking_doc_id,
            entry_type="analysis",
            agent=AGENT_NAME,
            status="running",
        )

    yield json.dumps({"type": "progress", "step": f"{agent_label} starting analysis", "agent": AGENT_NAME}) + "\n"

    # Build tools: MCP (ERP data) + local (computation)
    mcp_client = _build_mcp_client()
    local_tools = _build_local_tools(AGENT_NAME)

    if not mcp_client:
        logger.error(f"[{AGENT_NAME}] GATEWAY_ENDPOINT not set — MCP tools unavailable")
        yield json.dumps({"type": "error", "error": "MCP Gateway not configured", "agent": AGENT_NAME}) + "\n"
        return
    all_tools = [mcp_client, *local_tools]

    # Add Code Interpreter for agents that need computational analysis
    ci_tool = _build_code_interpreter(AGENT_NAME)
    if ci_tool:
        all_tools.append(ci_tool)

    # Tool descriptions are built dynamically by the Agent when it starts the MCP client.
    # Do NOT call mcp_client.start()/stop() here — it conflicts with Agent's own lifecycle.

    system_prompt = _get_system_prompt(AGENT_NAME)
    prompt = _get_prompt(AGENT_NAME, document_id, user_email)
    response_text = ""

    try:
        from config import settings
        from utils.progress_hooks import ProgressBridge

        model = BedrockModel(
            model_id=settings.bedrock_model_id,
            streaming=False,
        )

        bridge = ProgressBridge(AGENT_NAME)
        agent = Agent(
            model=model,
            tools=all_tools,
            system_prompt=system_prompt,
            hooks=[bridge.hook_provider],
        )

        # Run agent in thread, stream per-tool progress events in real-time
        async for event_line in bridge.run(agent, prompt):
            yield event_line
        result = bridge.result

        if hasattr(result, 'message') and result.message:
            for block in result.message.get("content", []):
                if isinstance(block, dict) and "text" in block:
                    response_text += block["text"]
                elif isinstance(block, str):
                    response_text += block
        if not response_text:
            response_text = str(result)
        tools_used = []

        # Parse JSON from response
        json_str = response_text
        if "```json" in json_str:
            json_str = json_str.split("```json")[1].split("```")[0]
        elif "```" in json_str:
            json_str = json_str.split("```")[1].split("```")[0]

        try:
            parsed = json.loads(json_str.strip())
        except json.JSONDecodeError:
            parsed = {"raw_response": response_text, "confidence": 0.0}

        # Standalone analysis is READ-ONLY — no auto_approve, no decisions.
        # Record in runs[] for history (visible in MR modal tree) but not as a decision.
        if document_id and isinstance(parsed, dict) and not parsed.get("error"):
            try:
                from services.lifecycle import add_run_entry, get_lifecycle_by_po, update_run_entry

                rec = parsed.get("recommendation", parsed.get("match_result", parsed.get("validation_result", "")))
                conf = _safe_float(parsed.get("confidence", 0))
                reasoning = parsed.get("reasoning", parsed.get("summary", ""))

                if _agent_run_id:
                    # Update the "running" entry created at invocation start
                    update_run_entry(
                        document_id=_tracking_doc_id,
                        run_id=_agent_run_id,
                        status="completed",
                        recommendation=str(rec),
                        confidence=conf,
                        summary=str(reasoning) if reasoning else "",
                        result=parsed,
                    )
                else:
                    # Agents without duplicate tracking — create entry as before
                    tracking_doc_id = document_id
                    if AGENT_NAME in ("receiving", "invoice_matching", "payment"):
                        po_id = payload.get("order_id", "")
                        if not po_id:
                            po_id = (parsed.get("order_id", "")
                                     or parsed.get("purchase_order", "")
                                     or parsed.get("po_number", ""))
                        if po_id:
                            mr_lifecycle = get_lifecycle_by_po(po_id)
                            if mr_lifecycle:
                                tracking_doc_id = mr_lifecycle["document_id"]
                                logger.info(f"[{AGENT_NAME}] Tracking run under MR {tracking_doc_id} (PO: {po_id})")

                    add_run_entry(
                        document_id=tracking_doc_id,
                        entry_type="analysis",
                        agent=AGENT_NAME,
                        status="completed",
                        recommendation=str(rec),
                        confidence=conf,
                        summary=str(reasoning) if reasoning else "",
                        result=parsed,
                    )
            except Exception as e:
                logger.warning(f"[{AGENT_NAME}] Failed to record standalone run: {e}")
        logger.info(f"[{AGENT_NAME}] completed (read-only), keys: {list(parsed.keys()) if isinstance(parsed, dict) else type(parsed)}")

    except Exception as e:
        import traceback
        logger.error(f"[{AGENT_NAME}] failed: {e}\n{traceback.format_exc()}")
        parsed = {"error": str(e), "reasoning": f"Agent error: {e}", "confidence": 0.0}
        tools_used = []
        # Mark running entry as failed
        if _agent_run_id:
            try:
                from services.lifecycle import update_run_entry
                update_run_entry(_tracking_doc_id, _agent_run_id, status="failed")
            except Exception:
                pass  # nosec B110 -- best-effort lifecycle update inside an outer error handler

    # Validate agent reasoning against procurement policy (Automated Reasoning)
    ar_validation = _validate_with_guardrail(response_text)
    if ar_validation.get("findings"):
        logger.info(f"[{AGENT_NAME}] AR findings: {len(ar_validation['findings'])}")

    duration = round(time.time() - start_time, 1)
    metrics = {"total_duration_s": duration}

    yield json.dumps({
        "type": "result",
        "agent": AGENT_NAME,
        "document_id": document_id,
        "result": parsed,
        "ar_validation": ar_validation,
        "tools_used": tools_used,
        "progress_steps": [],
        "metrics": metrics,
    }) + "\n"


def _safe_float(val) -> float:
    try:
        return float(val or 0)
    except (TypeError, ValueError):
        return 0.0


async def _run_workflow(document_id: str, user_email: str = "", resume_from: str = ""):
    """Run the checkpointed workflow through the existing AgentCore NDJSON stream."""
    from config import settings
    from utils import workflow_telemetry as telemetry
    from workflow.p2p_graph import open_checkpointer, stream_workflow
    from workflow.strands_runtime import StrandsWorkflowRuntime

    runtime = StrandsWorkflowRuntime(
        _build_mcp_client,
        lambda: BedrockModel(model_id=settings.bedrock_model_id, streaming=False),
        _get_system_prompt,
        _build_local_tools,
    )
    try:
        async with open_checkpointer() as saver:
            async for event in stream_workflow(
                document_id, user_email, resume_from, runtime, saver,
                lambda result: _post_process("requisition", result),
            ):
                yield json.dumps(event) + "\n"
    except Exception:
        logger.exception("Could not open workflow checkpoint store")
        telemetry.record("checkpoint_open_failed", metrics={"CheckpointFailures": 1}, error_code="WORKFLOW_CHECKPOINT_ERROR")
        yield json.dumps({
            "type": "error", "agent": "workflow", "error": "WORKFLOW_CHECKPOINT_ERROR",
            "message": "The workflow checkpoint store is unavailable.",
        }) + "\n"


if __name__ == "__main__":
    app.run()
