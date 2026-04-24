"""Tests for WP-607a — Capabilities Hub SVAS bridge (svas_interface.py).

Covers:
  - Mock fallback paths (unreachable, unhealthy, submit failure)
  - _response_to_svas translation (Temporal up + down)
  - _is_caphub_reachable TCP probe
  - _is_caphub_healthy HTTP probe (all accepted status values)
  - _submit_provision_request happy path + error path + body correctness
  - analyze_intent integration (full happy path + each fallback branch)
"""
import json
import sys
import os
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import pytest

# Ensure the Capabilities Hub root is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import svas_interface as bridge


# ── helpers ────────────────────────────────────────────────────────────────────

def _mock_urlopen(response_body: dict):
    """Build a context-manager mock for urlopen."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(response_body).encode()
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


# ── _mock_response ─────────────────────────────────────────────────────────────

def test_mock_response_returns_tuple():
    analysis, agent, steps = bridge._mock_response("wf-1", "test intent", "offline")
    assert isinstance(analysis, str)
    assert agent == "Capabilities Hub"
    assert isinstance(steps, list)
    assert len(steps) >= 1


def test_mock_response_includes_reason():
    analysis, _, _ = bridge._mock_response("wf-1", "provision tools", "Capabilities Hub not reachable")
    assert "Capabilities Hub not reachable" in analysis


def test_mock_response_truncates_intent():
    long_intent = "B" * 200
    analysis, _, _ = bridge._mock_response("wf-x", long_intent, "offline")
    assert len(analysis) < 400


def test_mock_response_steps_have_id_and_label():
    _, _, steps = bridge._mock_response("wf-1", "intent", "reason")
    for step in steps:
        assert "id" in step
        assert "label" in step


def test_mock_response_has_three_steps():
    _, _, steps = bridge._mock_response("wf-1", "intent", "reason")
    assert len(steps) == 3


# ── _response_to_svas ──────────────────────────────────────────────────────────

def test_response_to_svas_temporal_up():
    response = {"status": "submitted", "workflowId": "cap-wf-abc"}
    analysis, agent, steps = bridge._response_to_svas(response, "provision tools", "wf-2")
    assert agent == "Capabilities Hub"
    assert "cap-wf-abc" in analysis
    assert "connected" in analysis


def test_response_to_svas_temporal_up_has_four_steps():
    response = {"status": "submitted", "workflowId": "cap-wf-abc"}
    _, _, steps = bridge._response_to_svas(response, "provision tools", "wf-2")
    assert len(steps) == 4


def test_response_to_svas_temporal_up_step_labels():
    response = {"status": "submitted", "workflowId": "cap-wf-abc"}
    _, _, steps = bridge._response_to_svas(response, "provision tools", "wf-2")
    labels = [s["label"] for s in steps]
    assert any("Identify Required Utilities" in l for l in labels)
    assert any("Validate Tool Availability" in l for l in labels)
    assert any("Provision Specialized Toolchain" in l for l in labels)
    assert any("Expose Capabilities ABI" in l for l in labels)


def test_response_to_svas_temporal_down():
    response = {"status": "temporal_unavailable", "workflowId": "cap-wf-xyz"}
    analysis, agent, steps = bridge._response_to_svas(response, "provision tools", "wf-3")
    assert "offline" in analysis
    assert agent == "Capabilities Hub"


def test_response_to_svas_temporal_down_has_three_steps():
    response = {"status": "error", "workflowId": "cap-wf-xyz"}
    _, _, steps = bridge._response_to_svas(response, "provision tools", "wf-3")
    assert len(steps) == 3


def test_response_to_svas_temporal_down_step_labels():
    response = {"status": "error", "workflowId": "cap-wf-xyz"}
    _, _, steps = bridge._response_to_svas(response, "provision tools", "wf-3")
    labels = [s["label"] for s in steps]
    assert any("local" in l.lower() for l in labels)


def test_response_to_svas_falls_back_to_workflow_id():
    response = {"status": "submitted"}  # no workflowId key
    analysis, _, _ = bridge._response_to_svas(response, "intent", "wf-fallback")
    assert "wf-fallback" in analysis


# ── _is_caphub_reachable ───────────────────────────────────────────────────────

def test_is_caphub_reachable_success():
    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    with patch("socket.create_connection", return_value=mock_conn):
        assert bridge._is_caphub_reachable() is True


def test_is_caphub_reachable_failure():
    with patch("socket.create_connection", side_effect=OSError("refused")):
        assert bridge._is_caphub_reachable() is False


def test_is_caphub_reachable_timeout():
    with patch("socket.create_connection", side_effect=TimeoutError()):
        assert bridge._is_caphub_reachable() is False


# ── _is_caphub_healthy ─────────────────────────────────────────────────────────

def test_is_caphub_healthy_status_healthy():
    mock_resp = _mock_urlopen({"status": "healthy"})
    with patch("svas_interface.urlopen", return_value=mock_resp):
        assert bridge._is_caphub_healthy() is True


def test_is_caphub_healthy_status_ok():
    mock_resp = _mock_urlopen({"status": "ok"})
    with patch("svas_interface.urlopen", return_value=mock_resp):
        assert bridge._is_caphub_healthy() is True


def test_is_caphub_healthy_status_OK():
    mock_resp = _mock_urlopen({"status": "OK"})
    with patch("svas_interface.urlopen", return_value=mock_resp):
        assert bridge._is_caphub_healthy() is True


def test_is_caphub_healthy_status_operational():
    mock_resp = _mock_urlopen({"status": "operational"})
    with patch("svas_interface.urlopen", return_value=mock_resp):
        assert bridge._is_caphub_healthy() is True


def test_is_caphub_healthy_returns_false_on_degraded():
    mock_resp = _mock_urlopen({"status": "degraded"})
    with patch("svas_interface.urlopen", return_value=mock_resp):
        assert bridge._is_caphub_healthy() is False


def test_is_caphub_healthy_returns_false_on_error():
    with patch("svas_interface.urlopen", side_effect=URLError("connection refused")):
        assert bridge._is_caphub_healthy() is False


# ── _submit_provision_request ──────────────────────────────────────────────────

def test_submit_provision_request_success():
    payload = {"status": "submitted", "workflowId": "cap-wf-123"}
    mock_resp = _mock_urlopen(payload)
    with patch("svas_interface.urlopen", return_value=mock_resp):
        result = bridge._submit_provision_request("provision tools", "wf-9")
    assert result["status"] == "submitted"
    assert result["workflowId"] == "cap-wf-123"


def test_submit_provision_request_returns_none_on_error():
    with patch("svas_interface.urlopen", side_effect=URLError("timeout")):
        result = bridge._submit_provision_request("intent", "wf-10")
    assert result is None


def test_submit_provision_request_sends_correct_body():
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["data"] = json.loads(req.data.decode())
        captured["method"] = req.method
        return _mock_urlopen({"status": "submitted", "workflowId": "x"})

    with patch("svas_interface.urlopen", side_effect=fake_urlopen):
        bridge._submit_provision_request("my intent", "wf-abc")

    assert captured["data"]["intent"] == "my intent"
    assert captured["data"]["workflow_id"] == "svas-wf-abc"
    assert captured["method"] == "POST"


# ── analyze_intent (integration) ──────────────────────────────────────────────

def test_analyze_intent_mock_when_unreachable():
    with patch.object(bridge, "_is_caphub_reachable", return_value=False):
        analysis, agent, steps = bridge.analyze_intent("wf-11", "some intent")
    assert agent == "Capabilities Hub"
    assert "not reachable" in analysis


def test_analyze_intent_mock_when_unhealthy():
    with patch.object(bridge, "_is_caphub_reachable", return_value=True), \
         patch.object(bridge, "_is_caphub_healthy", return_value=False):
        analysis, agent, steps = bridge.analyze_intent("wf-12", "some intent")
    assert "unhealthy" in analysis


def test_analyze_intent_mock_when_submit_fails():
    with patch.object(bridge, "_is_caphub_reachable", return_value=True), \
         patch.object(bridge, "_is_caphub_healthy", return_value=True), \
         patch.object(bridge, "_submit_provision_request", return_value=None):
        analysis, agent, steps = bridge.analyze_intent("wf-13", "some intent")
    assert "provision request call failed" in analysis


def test_analyze_intent_live_path():
    fake_response = {"status": "submitted", "workflowId": "cap-wf-live"}
    with patch.object(bridge, "_is_caphub_reachable", return_value=True), \
         patch.object(bridge, "_is_caphub_healthy", return_value=True), \
         patch.object(bridge, "_submit_provision_request", return_value=fake_response):
        analysis, agent, steps = bridge.analyze_intent("wf-14", "provision toolchain")
    assert agent == "Capabilities Hub"
    assert "cap-wf-live" in analysis
    assert len(steps) == 4


def test_analyze_intent_context_ignored_safely():
    fake_response = {"status": "submitted", "workflowId": "cap-ctx"}
    with patch.object(bridge, "_is_caphub_reachable", return_value=True), \
         patch.object(bridge, "_is_caphub_healthy", return_value=True), \
         patch.object(bridge, "_submit_provision_request", return_value=fake_response):
        analysis, agent, steps = bridge.analyze_intent("wf-15", "intent", context={"key": "val"})
    assert agent == "Capabilities Hub"
