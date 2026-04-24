"""SVAS → Capabilities Hub bridge (WP-607a).

Translates a SVAS intent into a Capabilities Hub provision request by calling
the live Capabilities Hub API at /api/v1/capabilities/provision, then maps the
response back to the SVAS sub-agent tuple format: (analysis_reasoning,
target_agent, steps).

Role: Provisioner — surfaces required tooling, git helpers, merge utilities,
and specialized capabilities ABI for multi-agent workflows.  The Hub manages
tool inventory, validates availability, and exposes a stable ABI contract so
downstream agents can consume specialised utilities without hard-coding paths.

Flow:
  1. TCP probe  → is Capabilities Hub process running?
  2. GET /health → confirm API is healthy (not just the port)
  3. POST /api/v1/capabilities/provision  → submit intent as provision request
  4. Map response to (analysis, agent_name, steps)
  5. Deterministic mock fallback when Hub offline or Temporal not connected

Configuration (env vars):
  CAPHUB_API_URL  — default http://localhost:8007
  CAPHUB_TIMEOUT  — HTTP timeout in seconds, default 5
"""
from __future__ import annotations

import json
import logging
import os
import socket
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger("SVAS_CapHub_Bridge")

# ── Configuration ─────────────────────────────────────────────────────────────

_CAPHUB_BASE = os.getenv("CAPHUB_API_URL", "http://localhost:8007").rstrip("/")
_TIMEOUT     = float(os.getenv("CAPHUB_TIMEOUT", "5"))

# ── Connectivity probes ────────────────────────────────────────────────────────

def _is_caphub_reachable() -> bool:
    """TCP probe — fast check before making HTTP calls."""
    try:
        url = _CAPHUB_BASE.replace("http://", "").replace("https://", "")
        host, _, port_str = url.partition(":")
        port = int(port_str) if port_str else 80
        with socket.create_connection((host, port), timeout=1):
            return True
    except Exception:
        return False


def _is_caphub_healthy() -> bool:
    """GET /health — confirm the API layer is fully up."""
    try:
        req = Request(f"{_CAPHUB_BASE}/health", method="GET")
        with urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
            return data.get("status") in ("healthy", "ok", "OK", "operational")
    except Exception:
        return False


# ── API call ──────────────────────────────────────────────────────────────────

def _submit_provision_request(intent: str, workflow_id: str) -> dict | None:
    """POST /api/v1/capabilities/provision and return parsed JSON, or None on error."""
    try:
        body = json.dumps({
            "intent":      intent,
            "workflow_id": f"svas-{workflow_id}",
        }).encode()
        req = Request(
            f"{_CAPHUB_BASE}/api/v1/capabilities/provision",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        logger.warning("Capabilities Hub provision request failed: %s", exc)
        return None


# ── Response → SVAS translation ───────────────────────────────────────────────

def _response_to_svas(
    response: dict,
    intent: str,
    workflow_id: str,
) -> tuple[str, str, list]:
    """Translate a Capabilities Hub /provision response to SVAS tuple format."""
    status      = response.get("status", "submitted")
    wf_id       = response.get("workflowId", workflow_id)
    temporal_up = status == "submitted"

    analysis = (
        f"Capabilities Hub Provisioning (workflow={wf_id}, "
        f"temporal={'connected' if temporal_up else 'offline'}):\n"
        f"  Provision request submitted for tool surfacing and ABI exposure."
    )

    if temporal_up:
        steps = [
            {"id": "s1", "label": "CapHub: Identify Required Utilities"},
            {"id": "s2", "label": "CapHub: Validate Tool Availability"},
            {"id": "s3", "label": "CapHub: Provision Specialized Toolchain"},
            {"id": "s4", "label": "CapHub: Expose Capabilities ABI"},
        ]
    else:
        steps = [
            {"id": "s1", "label": "Identify Required Utilities (local)"},
            {"id": "s2", "label": "Provision Specialized Tools (local)"},
            {"id": "s3", "label": "Expose Capabilities ABI (local)"},
        ]

    return analysis, "Capabilities Hub", steps


# ── Mock fallback ─────────────────────────────────────────────────────────────

def _mock_response(intent: str, workflow_id: str, reason: str) -> tuple[str, str, list]:
    """Deterministic fallback when Capabilities Hub is offline."""
    logger.info("[%s] Capabilities Hub bridge using mock fallback: %s", workflow_id, reason)
    return (
        f"Capabilities Hub (offline — {reason}): "
        f"Surfacing required tooling for intent '{intent[:60]}'.",
        "Capabilities Hub",
        [
            {"id": "s1", "label": "Identify Required Utilities (local)"},
            {"id": "s2", "label": "Provision Specialized Tools (local)"},
            {"id": "s3", "label": "Expose Capabilities ABI (local)"},
        ],
    )


# ── Public SVAS interface ─────────────────────────────────────────────────────

def analyze_intent(
    workflow_id: str,
    intent: str,
    context: dict | None = None,
) -> tuple[str, str, list]:
    """
    Primary SVAS entry-point for Capabilities Hub persona.

    1. TCP probe — fast check if Capabilities Hub is listening.
    2. GET /health — confirm API is healthy.
    3. POST /api/v1/capabilities/provision — submit the intent as a provision request.
    4. Translate response to (analysis, agent_name, steps).
    5. Fallback to deterministic mock if any step fails.
    """
    if not _is_caphub_reachable():
        return _mock_response(intent, workflow_id, reason=f"Capabilities Hub not reachable at {_CAPHUB_BASE}")

    if not _is_caphub_healthy():
        return _mock_response(intent, workflow_id, reason="Capabilities Hub /health returned unhealthy")

    response = _submit_provision_request(intent, workflow_id)
    if response is None:
        return _mock_response(intent, workflow_id, reason="provision request call failed")

    return _response_to_svas(response, intent, workflow_id)
