"""Capabilities Hub FastAPI backend (WP-607a).

Manages the tool inventory, validates capability availability, and exposes
a stable ABI contract so downstream agents can consume specialised utilities
without hard-coding paths.

Role: Provisioner — surfaces required tooling, git helpers, merge utilities,
and specialized capabilities ABI for multi-agent workflows.

Endpoints:
  GET  /health                              — liveness probe
  POST /api/v1/capabilities/provision       — accept SVAS provision intent
  GET  /api/v1/capabilities/inventory       — list available capabilities
  GET  /api/v1/capabilities/provisions      — list provision requests
  GET  /api/v1/capabilities/audit           — provision audit log

Configuration (env vars):
  OPENROUTER_API_KEY    — LLM for capability matching
  CAPHUB_CORS_ORIGINS   — allowed CORS origins
  PORT                  — default 8007
"""
from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logger = logging.getLogger("CapHub_API")
logging.basicConfig(level=logging.INFO)

_DB = os.getenv("CAPHUB_DB_PATH", "capabilities_hub.db")
_OPENROUTER_KEY   = os.getenv("OPENROUTER_API_KEY", "")
_OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "anthropic/claude-3.5-haiku")
_OPENROUTER_BASE  = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

# Built-in capability catalog
_CAPABILITY_CATALOG = [
    {"id": "git-helper",        "name": "Git Helper",          "category": "vcs",        "available": True},
    {"id": "merge-util",        "name": "Merge Utility",       "category": "vcs",        "available": True},
    {"id": "code-scanner",      "name": "Code Scanner",        "category": "analysis",   "available": True},
    {"id": "ast-parser",        "name": "AST Parser",          "category": "analysis",   "available": True},
    {"id": "test-runner",       "name": "Test Runner",         "category": "qa",         "available": True},
    {"id": "deploy-gate",       "name": "Deploy Gate",         "category": "cicd",       "available": True},
    {"id": "doc-generator",     "name": "Doc Generator",       "category": "docs",       "available": True},
    {"id": "schema-validator",  "name": "Schema Validator",    "category": "data",       "available": True},
    {"id": "api-probe",         "name": "API Probe",           "category": "network",    "available": True},
    {"id": "llm-wrapper",       "name": "LLM Wrapper",         "category": "ai",         "available": bool(_OPENROUTER_KEY)},
]


# ── Database ──────────────────────────────────────────────────────────────────

def _init_db():
    conn = sqlite3.connect(_DB)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS cap_provisions (
            provision_id  TEXT PRIMARY KEY,
            workflow_id   TEXT,
            intent        TEXT,
            matched_caps  TEXT,  -- JSON array of capability ids
            status        TEXT DEFAULT 'provisioned',
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS cap_audit (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            provision_id TEXT,
            action      TEXT,
            details     TEXT,
            timestamp   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    conn.close()


def _db_query(sql: str, params: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Capability matching ───────────────────────────────────────────────────────

def _match_capabilities(intent: str) -> list[dict]:
    """Keyword-based capability matching with all available tools as fallback."""
    intent_lower = intent.lower()
    keyword_map = {
        "git":       ["git-helper", "merge-util"],
        "merge":     ["merge-util", "git-helper"],
        "code":      ["code-scanner", "ast-parser"],
        "test":      ["test-runner"],
        "deploy":    ["deploy-gate"],
        "doc":       ["doc-generator"],
        "schema":    ["schema-validator"],
        "api":       ["api-probe"],
        "llm":       ["llm-wrapper"],
        "analyze":   ["code-scanner", "ast-parser"],
        "scan":      ["code-scanner"],
    }
    matched_ids: set[str] = set()
    for kw, cap_ids in keyword_map.items():
        if kw in intent_lower:
            matched_ids.update(cap_ids)

    if not matched_ids:
        matched_ids = {c["id"] for c in _CAPABILITY_CATALOG if c["available"]}

    return [c for c in _CAPABILITY_CATALOG if c["id"] in matched_ids]


# ── App lifecycle ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def _lifespan(app: FastAPI):
    _init_db()
    logger.info("Capabilities Hub started — DB: %s, catalog: %d tools", _DB, len(_CAPABILITY_CATALOG))
    yield
    logger.info("Capabilities Hub stopped.")


app = FastAPI(title="Capabilities Hub", version="1.0.0", lifespan=_lifespan)

_raw_origins = os.getenv("CAPHUB_CORS_ORIGINS", "http://localhost:5173,http://localhost:3000")
_CORS_ORIGINS = [o.strip() for o in _raw_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


# ── Schemas ───────────────────────────────────────────────────────────────────

class ProvisionRequest(BaseModel):
    intent: str
    workflow_id: str
    context: dict = {}


class ProvisionResponse(BaseModel):
    status: str
    provision_id: str
    workflow_id: str
    capabilities_provisioned: list[dict]
    abi_contract: str
    timestamp: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {
        "status": "healthy",
        "system": "Capabilities Hub",
        "catalog_size": len(_CAPABILITY_CATALOG),
        "llm_available": bool(_OPENROUTER_KEY),
    }


@app.post("/api/v1/capabilities/provision", response_model=ProvisionResponse)
def provision(req: ProvisionRequest):
    provision_id = f"prov-{uuid.uuid4().hex[:8]}"
    matched = _match_capabilities(req.intent)
    matched_ids = [c["id"] for c in matched]

    import json
    conn = sqlite3.connect(_DB)
    conn.execute(
        "INSERT INTO cap_provisions (provision_id, workflow_id, intent, matched_caps) "
        "VALUES (?, ?, ?, ?)",
        (provision_id, req.workflow_id, req.intent, json.dumps(matched_ids)),
    )
    conn.execute(
        "INSERT INTO cap_audit (provision_id, action, details) VALUES (?, ?, ?)",
        (provision_id, "provision_created",
         json.dumps({"intent": req.intent[:80], "caps": matched_ids})),
    )
    conn.commit()
    conn.close()

    abi = (
        f"ABI Contract {provision_id}: {len(matched)} capabilities provisioned "
        f"for workflow {req.workflow_id}. Tools: {', '.join(matched_ids)}."
    )

    logger.info("[%s] Provisioned %d capabilities for: %s", provision_id, len(matched), req.intent[:60])
    return ProvisionResponse(
        status="submitted",
        provision_id=provision_id,
        workflow_id=req.workflow_id,
        capabilities_provisioned=matched,
        abi_contract=abi,
        timestamp=datetime.utcnow().isoformat(),
    )


@app.get("/api/v1/capabilities/inventory")
def inventory():
    return {"capabilities": _CAPABILITY_CATALOG, "count": len(_CAPABILITY_CATALOG)}


@app.get("/api/v1/capabilities/provisions")
def list_provisions(limit: int = 50):
    rows = _db_query(
        "SELECT provision_id, workflow_id, intent, status, created_at "
        "FROM cap_provisions ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    return {"provisions": rows, "count": len(rows)}


@app.get("/api/v1/capabilities/audit")
def audit(limit: int = 100):
    rows = _db_query(
        "SELECT provision_id, action, details, timestamp "
        "FROM cap_audit ORDER BY timestamp DESC LIMIT ?",
        (limit,),
    )
    return {"entries": rows, "count": len(rows)}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8007"))
    uvicorn.run(app, host="0.0.0.0", port=port)
