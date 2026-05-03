"""Capabilities Hub FastAPI backend (WP-607a).

Role: Provisioner + Registry — surfaces tooling ABI and acts as the
authoritative service registry for the SVAS fleet.

Endpoints:
  GET  /health                              — liveness probe
  POST /api/v1/capabilities/provision       — accept SVAS provision intent
  GET  /api/v1/capabilities/inventory       — list available capabilities
  GET  /api/v1/capabilities/provisions      — list provision requests
  GET  /api/v1/capabilities/audit           — provision audit log
  POST /registry/register                   — register an arm/service
  POST /registry/recommend                  — recommend arms for an intent
  GET  /registry/gaps                       — identify coverage gaps
  GET  /registry/health                     — health of all registered services

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
            matched_caps  TEXT,
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
        CREATE TABLE IF NOT EXISTS registry (
            arm_key     TEXT PRIMARY KEY,
            label       TEXT,
            service_url TEXT,
            domain      TEXT,
            keywords    TEXT,
            priority    INTEGER DEFAULT 50,
            status      TEXT DEFAULT 'registered',
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen   TIMESTAMP
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


# ── Registry schemas ──────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    arm_key: str
    label: str
    service_url: str
    domain: str
    keywords: list[str] = []
    priority: int = 50


class RecommendRequest(BaseModel):
    intent: str
    workflow_id: str = ""
    top_k: int = 3


class RecommendMatch(BaseModel):
    arm_key: str
    label: str
    service_url: str
    domain: str
    confidence: float
    reason: str


# ── Registry endpoints ────────────────────────────────────────────────────────

@app.post("/registry/register")
def registry_register(req: RegisterRequest):
    import json
    conn = sqlite3.connect(_DB)
    now = datetime.utcnow().isoformat()
    conn.execute(
        """INSERT INTO registry (arm_key, label, service_url, domain, keywords, priority, last_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(arm_key) DO UPDATE SET
             label=excluded.label, service_url=excluded.service_url,
             domain=excluded.domain, keywords=excluded.keywords,
             priority=excluded.priority, last_seen=excluded.last_seen, status='registered'""",
        (req.arm_key, req.label, req.service_url, req.domain,
         json.dumps(req.keywords), req.priority, now),
    )
    conn.commit()
    conn.close()
    logger.info("Registry: arm '%s' registered — %s", req.arm_key, req.service_url)
    return {"status": "registered", "arm_key": req.arm_key, "registered_at": now}


@app.post("/registry/recommend")
def registry_recommend(req: RecommendRequest):
    import json
    rows = _db_query(
        "SELECT arm_key, label, service_url, domain, keywords, priority FROM registry WHERE status='registered'",
    )
    intent_lower = req.intent.lower()
    scored: list[tuple[float, dict]] = []
    for row in rows:
        kws = json.loads(row.get("keywords") or "[]")
        hits = sum(1 for k in kws if k.lower() in intent_lower)
        if hits == 0:
            continue
        conf = min(1.0, round(hits / max(len(kws), 1), 2))
        scored.append((conf, row, hits))

    scored.sort(key=lambda x: (x[0], x[2]), reverse=True)
    results = []
    for conf, row, hits in scored[: req.top_k]:
        results.append(RecommendMatch(
            arm_key=row["arm_key"],
            label=row["label"],
            service_url=row["service_url"],
            domain=row["domain"],
            confidence=conf,
            reason=f"{hits} keyword(s) matched in intent",
        ))

    logger.info("[%s] Recommend: %d arms matched for intent", req.workflow_id, len(results))
    return {
        "workflow_id": req.workflow_id,
        "intent_snippet": req.intent[:80],
        "recommendations": [r.model_dump() for r in results],
        "count": len(results),
    }


@app.get("/registry/gaps")
def registry_gaps():
    rows = _db_query("SELECT domain, COUNT(*) as cnt FROM registry WHERE status='registered' GROUP BY domain")
    covered = {r["domain"] for r in rows}
    all_domains = {"compliance", "governance", "ml-ops", "change-mgmt", "data-quality",
                   "security", "infrastructure", "project-mgmt", "knowledge", "analytics"}
    gaps = sorted(all_domains - covered)
    return {
        "covered_domains": sorted(covered),
        "gap_domains": gaps,
        "coverage_pct": round(len(covered) / len(all_domains) * 100, 1),
        "registered_arms": len(_db_query("SELECT arm_key FROM registry WHERE status='registered'")),
    }


@app.get("/registry/health")
def registry_health():
    import json, urllib.request
    rows = _db_query(
        "SELECT arm_key, label, service_url, last_seen FROM registry WHERE status='registered'",
    )
    results = []
    for row in rows:
        url = row["service_url"].rstrip("/") + "/health" if row["service_url"] else ""
        status = "unknown"
        if url:
            try:
                with urllib.request.urlopen(url, timeout=4) as resp:
                    status = "healthy" if resp.status == 200 else f"http-{resp.status}"
            except Exception:
                status = "unreachable"
        results.append({
            "arm_key": row["arm_key"],
            "label": row["label"],
            "service_url": row["service_url"],
            "health": status,
            "last_seen": row["last_seen"],
        })
    healthy = sum(1 for r in results if r["health"] == "healthy")
    return {
        "arms": results,
        "healthy": healthy,
        "total": len(results),
        "hub_status": "healthy",
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8007"))
    uvicorn.run(app, host="0.0.0.0", port=port)
