"""Vera challenge bot — HTTP server.

Run:  uvicorn bot:app --host 0.0.0.0 --port 8080

Proactive path: context -> tick -> composed message (vera/engine.py).
Reactive path: /v1/reply -> rule classifier -> stage machine (vera/reply.py).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from vera.compose import compose  # re-exported: the §7 contract  # noqa: F401
from vera.engine import plan_tick
from vera.reply import respond
from vera.store import SCOPES, Store

VERSION = "1.1.0"
log = logging.getLogger("vera")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
START = time.time()
store = Store()
app = FastAPI(title="Vera challenge bot", version=VERSION)


@app.exception_handler(RequestValidationError)
async def _malformed(request: Request, exc: RequestValidationError):
    """Brief §2.1: malformed requests get 400 with a reason, never FastAPI's default 422."""
    details = "; ".join(f"{'.'.join(str(x) for x in e.get('loc', [])[1:]) or 'body'}: {e.get('msg')}"
                        for e in exc.errors())[:500]
    if request.url.path.endswith("/v1/context"):
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "malformed", "details": details})
    return JSONResponse(status_code=400, content={"error": "malformed", "details": details})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@app.get("/v1/healthz")
async def healthz() -> dict:
    return {"status": "ok", "uptime_seconds": int(time.time() - START), "contexts_loaded": store.counts()}


def _team() -> dict:
    """Env vars win; otherwise team.json next to bot.py."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "team.json"), encoding="utf-8") as f:
            t = json.load(f)
    except (OSError, ValueError):
        t = {}
    members = os.environ.get("TEAM_MEMBERS")
    return {
        "team_name": os.environ.get("TEAM_NAME") or t.get("team_name") or "unset",
        "team_members": [m.strip() for m in members.split(",") if m.strip()] if members else (t.get("team_members") or []),
        "contact_email": os.environ.get("CONTACT_EMAIL") or t.get("contact_email") or "unset",
        "submitted_at": os.environ.get("SUBMITTED_AT") or t.get("submitted_at") or "",
    }


@app.get("/v1/metadata")
async def metadata() -> dict:
    team = _team()
    return {
        "team_name": team["team_name"],
        "team_members": team["team_members"],
        "model": os.environ.get("BOT_MODEL", "deterministic-templates"),
        "approach": "deterministic: facts-sheet grounding, one-signal decision layer, trigger-family playbooks, consent-aware customer composer, validator, history-aware tick policy, rule-based reply engine",
        "contact_email": team["contact_email"],
        "version": VERSION,
        "submitted_at": team["submitted_at"],
    }


class ContextBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str | None = None


@app.post("/v1/context")
async def push_context(body: ContextBody):
    if body.scope not in SCOPES:
        return JSONResponse(status_code=400, content={
            "accepted": False, "reason": "invalid_scope", "details": f"scope must be one of {SCOPES}"})
    if not body.context_id.strip():
        return JSONResponse(status_code=400, content={
            "accepted": False, "reason": "invalid_context_id", "details": "context_id must be a non-empty string"})
    if body.version < 0:
        return JSONResponse(status_code=400, content={
            "accepted": False, "reason": "invalid_version", "details": "version must be >= 0"})
    status, current = store.put(body.scope, body.context_id, body.version, body.payload)
    if status == "stale":
        return JSONResponse(status_code=409, content={
            "accepted": False, "reason": "stale_version", "current_version": current})
    # same version re-posted = idempotent no-op, still accepted
    return {"accepted": True, "ack_id": f"ack_{body.context_id}_v{body.version}",
            "stored_at": _now(), **({"duplicate": True} if status == "duplicate" else {})}


class TickBody(BaseModel):
    now: str | None = None                       # missing/unparseable -> wall clock
    available_triggers: list[str] | None = None  # null -> []


@app.post("/v1/tick")
async def tick(body: TickBody) -> dict:
    # Never 500: a crash here would be scored as malformed. Sending nothing is always valid.
    try:
        actions, decisions = plan_tick(store, body.now or _now(), body.available_triggers or [])
        log.info("tick now=%s triggers=%d actions=%d", body.now, len(body.available_triggers or []), len(actions))
        for d in decisions:
            log.debug("decision %s", d)
        return {"actions": actions}
    except Exception:
        log.exception("tick failed; returning no actions")
        return {"actions": []}


class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: str | None = None
    customer_id: str | None = None
    from_role: str | None = "merchant"
    message: str | None = ""
    received_at: str | None = None
    turn_number: int | None = 0


@app.post("/v1/reply")
async def reply(body: ReplyBody) -> dict:
    try:
        return _reply(body)
    except Exception:
        log.exception("reply failed; backing off")
        return {"action": "wait", "wait_seconds": 1800, "rationale": "Internal error; backing off instead of guessing."}


def _reply(body: ReplyBody) -> dict:
    body.message = body.message or ""
    body.from_role = body.from_role or "merchant"
    body.turn_number = body.turn_number or 0
    return respond(store, body)


@app.post("/v1/teardown")
async def teardown() -> dict:
    store.wipe()
    return {"wiped": True}
