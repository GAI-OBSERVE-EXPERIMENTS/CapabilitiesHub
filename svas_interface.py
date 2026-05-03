"""SVAS → Capabilities Hub — live service registry + provisioning.

Primary:  POST {CAPHUB_URL}/registry/recommend  — intent-to-arm recommendation
          POST {CAPHUB_URL}/api/v1/capabilities/provision — provision capabilities
Fallback: Claude AI advisory via OpenRouter.
Env var:  CAPHUB_URL
"""
from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger("SVAS_CapHub")

_CORPORATE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CORPORATE not in sys.path:
    sys.path.insert(0, _CORPORATE)

from ai_executor import execute_agent_task, enhanced_fallback, call_service

_AGENT_NAME  = "Capabilities Hub (Service Registry)"
_SERVICE_URL = os.getenv("CAPHUB_URL", "").rstrip("/")

_SYSTEM_PROMPT = """
You are the Capabilities Hub — the tooling and capability provisioning AI agent
in the SVAS fleet. You identify and coordinate the tools, SDKs, APIs, and
infrastructure capabilities needed to execute intents.

For the given intent, produce:
- Required capabilities: what specific tools, libraries, APIs, services, or
  infrastructure components are needed? List with version constraints if relevant.
- Availability assessment: categorise each as AVAILABLE (in fleet), NEEDS
  PROVISIONING, or BLOCKED (requires procurement/approval).
- Provisioning plan: for capabilities marked NEEDS PROVISIONING, what are the
  setup steps? Consider cost (zero-cost directive applies), time, and dependencies.
- ABI / interface contract: what interfaces or contracts must be exposed for
  downstream agents to consume these capabilities?
- Gaps: what capabilities are missing from the fleet that cannot be self-provisioned?

analysis must reference the specific tools/capabilities implied by the intent.
Steps must represent real capability identification and provisioning workflow stages.
"""


def analyze_intent(
    workflow_id: str,
    intent: str,
    context: dict | None = None,
) -> tuple[str, str, list]:
    context = context or {}

    if _SERVICE_URL:
        try:
            # First: get arm recommendations from the registry
            rec_data = call_service(
                f"{_SERVICE_URL}/registry/recommend",
                {"intent": intent, "workflow_id": workflow_id, "top_k": 3},
            )
            recommendations = rec_data.get("recommendations", [])

            # Then: provision capabilities for this workflow
            prov_data = call_service(
                f"{_SERVICE_URL}/api/v1/capabilities/provision",
                {"intent": intent, "workflow_id": workflow_id, "context": context},
            )
            prov_id   = prov_data.get("provision_id", "")
            caps      = prov_data.get("capabilities_provisioned", [])
            abi       = prov_data.get("abi_contract", "")

            rec_labels = [r.get("label", r.get("arm_key", "")) for r in recommendations]
            analysis = (
                f"Capabilities Hub provision {prov_id} complete for workflow {workflow_id}. "
                f"{len(caps)} tool(s) provisioned. "
                f"Registry recommends: {', '.join(rec_labels) or 'no specific arms matched'}. "
                f"{abi[:100]}"
            )
            steps = [
                {"id": "s1", "label": f"Provision {prov_id} — {len(caps)} capability(ies)"},
                {"id": "s2", "label": f"Registry: {len(recommendations)} arm(s) recommended"},
            ] + [
                {"id": f"s{i+3}", "label": f"Arm: {r.get('label','')} ({r.get('domain','')} — {int(r.get('confidence',0)*100)}%)"}
                for i, r in enumerate(recommendations[:3])
            ]
            logger.info("[%s] CapHub real execution OK — prov=%s caps=%d", workflow_id, prov_id, len(caps))
            return analysis, _AGENT_NAME, steps
        except Exception as exc:
            logger.warning("[%s] CapHub service unreachable (%s) — AI fallback", workflow_id, exc)

    try:
        result = execute_agent_task(_SYSTEM_PROMPT, intent, workflow_id, "capabilities_hub")
        return result["analysis"], _AGENT_NAME, result["steps"]
    except Exception as exc:
        logger.error("[%s] CapHub AI execution failed: %s", workflow_id, exc)
        return enhanced_fallback(_AGENT_NAME, "capabilities_hub", intent, workflow_id, str(exc))
