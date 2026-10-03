import time
import os
import json
import uuid
import asyncio
from typing import Dict, Any, List, Optional
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from src.config import PORT, logger, recent_terminal_logs, log_subscribers, web_terminal_handler
from src.models import ChatCompletionRequest
from src.db import (
    init_db,
    get_db_connection,
    get_setting,
    set_setting,
    log_admin_audit,
    get_admin_models,
    add_model,
    update_model,
    toggle_model,
    delete_model,
    get_admin_keys,
    save_key,
    toggle_key,
    delete_key,
    get_model_usage,
    clear_cooldown,
    get_admin_analytics,
    get_recent_traces,
    get_audit_logs,
    log_request_trace,
)
from src.triage import grade_prompt_difficulty
from src.proxy import execute_proxy_request
import logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure uvicorn logs are forwarded to web terminal
    for uvi_name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        uvi_logger = logging.getLogger(uvi_name)
        if web_terminal_handler not in uvi_logger.handlers:
            uvi_logger.addHandler(web_terminal_handler)

    logger.info(f"[SERVER INIT] Bootstrapping LLMProxy on port {PORT}...")
    await init_db()
    logger.info("[SERVER READY] Gateway is online and ready to accept requests.")
    yield
    logger.info("[SERVER SHUTDOWN] Gateway shutting down cleanly.")



app = FastAPI(
    title="LLMProxy",
    version="1.0.0",
    lifespan=lifespan
)

static_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
@app.get("/admin")
async def serve_admin_dashboard():
    """Serves the Admin Control Center Dashboard UI."""
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return JSONResponse(content={"status": "Gateway online", "dashboard": "static/index.html not found"})


@app.post("/v1/chat/completions")
async def proxy_endpoint(payload: ChatCompletionRequest):
    """
    OpenAI-compatible Chat Completions Endpoint.
    Supports dynamic BwK routing ('auto'), direct virtual triage ('triage'), and pinned direct models.
    """
    if payload.model == "triage":
        logger.info("[MAIN ROUTE] Direct virtual triage route requested.")
        req_id = f"req-triage-{uuid.uuid4().hex[:12]}"
        difficulty = await grade_prompt_difficulty(payload.messages)
        triage_content = {
            "difficulty": difficulty,
            "system_prompt_reference": "1=casual, 2=editing, 3=multi-step, 4=reasoning/summarization, 5=complex-system-design"
        }
        await log_request_trace(
            request_id=req_id,
            triage_difficulty=difficulty,
            has_tools=bool(payload.tools),
            final_platform="internal",
            final_model_id="triage",
            final_status=200,
            finish_reason="stop",
            tokens_output=len(str(triage_content)) // 4,
            response_preview=str(triage_content)[:200],
            attempts_detail=json.dumps([{"attempt": 1, "route": "internal/triage", "status": 200}])
        )
        return JSONResponse(content={
            "id": f"chatcmpl-triage-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "triage",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": str(triage_content)
                    },
                    "finish_reason": "stop"
                }
            ]
        })

    elif payload.model == "auto":
        logger.info("[MAIN ROUTE] Dynamic BwK auto route requested. Grading prompt difficulty...")
        difficulty = await grade_prompt_difficulty(payload.messages)
        logger.info(f"[MAIN ROUTE] Prompt difficulty graded as Difficulty={difficulty}. Delegating to failover proxy...")
        return await execute_proxy_request(payload, difficulty=difficulty)

    else:
        logger.info(f"[MAIN ROUTE] Pinned direct route requested for model='{payload.model}'. Delegating to failover proxy...")
        return await execute_proxy_request(payload, difficulty=None)


@app.get("/v1/models")
async def get_models():
    """Exposes only the virtual 'auto' model in the public model catalog list."""
    return {
        "object": "list",
        "data": [
            {
                "id": "auto",
                "object": "model",
                "created": 1677610602,
                "owned_by": "gateway",
                "permission": [],
                "root": "auto",
                "parent": None
            }
        ]
    }


@app.get("/api/admin/stats")
async def get_admin_stats():
    """Returns gateway aggregate usage, TTFT, and capacity pool metrics."""
    async with get_db_connection() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(tokens_input + tokens_output), 0), COALESCE(AVG(ttft_ms), 0) FROM usage_log"
        )
        row = await cur.fetchone()
        total_requests, total_tokens, avg_ttft_ms = row[0] or 0, row[1] or 0, round(row[2] or 0, 1)

        cur_cd = await conn.execute("SELECT COUNT(DISTINCT model_id) FROM rate_limit_cooldowns WHERE expires_at > datetime('now')")
        active_cooldowns = (await cur_cd.fetchone())[0] or 0

        cur_cd_set = await conn.execute("SELECT DISTINCT model_id FROM rate_limit_cooldowns WHERE expires_at > datetime('now')")
        cooldown_set = set(r[0] for r in await cur_cd_set.fetchall())

    all_models = await get_admin_models()

    total_capacity_rpm = 0
    current_used_rpm = 0
    total_capacity_rpd = 0
    current_used_rpd = 0
    total_capacity_tpm = 0
    current_used_tpm = 0
    total_capacity_tpd = 0
    current_used_tpd = 0
    healthy_models = 0

    seen_groups = set()
    for m in all_models:
        if not m["enabled"]:
            continue
        if m["model_id"] not in cooldown_set:
            healthy_models += 1

        group_key = m["shared_quota_group"] or f"{m['platform']}:{m['model_id']}"
        if group_key not in seen_groups:
            seen_groups.add(group_key)
            rpm, rpd, tpm, tpd = await get_model_usage(m["model_id"], m["shared_quota_group"])
            if m["rpm_limit"]:
                total_capacity_rpm += m["rpm_limit"]
                current_used_rpm += rpm
            if m["rpd_limit"]:
                total_capacity_rpd += m["rpd_limit"]
                current_used_rpd += rpd
            if m["tpm_limit"]:
                total_capacity_tpm += m["tpm_limit"]
                current_used_tpm += tpm
            if m["tpd_limit"]:
                total_capacity_tpd += m["tpd_limit"]
                current_used_tpd += tpd

    return {
        "total_requests": total_requests,
        "total_tokens": total_tokens,
        "avg_ttft_ms": avg_ttft_ms,
        "active_cooldowns": active_cooldowns,
        "healthy_models": healthy_models,
        "total_capacity_rpm": total_capacity_rpm,
        "current_used_rpm": current_used_rpm,
        "total_capacity_rpd": total_capacity_rpd,
        "current_used_rpd": current_used_rpd,
        "total_capacity_tpm": total_capacity_tpm,
        "current_used_tpm": current_used_tpm,
        "total_capacity_tpd": total_capacity_tpd,
        "current_used_tpd": current_used_tpd
    }


@app.get("/api/admin/models")
async def get_admin_models_endpoint():
    """Returns full catalog model details including live RPM, RPD, TPM, and TPD usages and cooldown expiry."""
    async with get_db_connection() as conn:
        cur_cd = await conn.execute(
            "SELECT platform, model_id, expires_at FROM rate_limit_cooldowns WHERE expires_at > datetime('now')"
        )
        cooldown_map = {(r[0], r[1]): r[2] for r in await cur_cd.fetchall()}

    models = await get_admin_models()
    result = []
    for m in models:
        rpm, rpd, tpm, tpd = await get_model_usage(m["model_id"], m["shared_quota_group"])
        cd_expiry = cooldown_map.get((m["platform"], m["model_id"]))
        result.append({
            **m,
            "current_rpm": rpm,
            "current_rpd": rpd,
            "current_tpm": tpm,
            "current_tpd": tpd,
            "is_cooldown": cd_expiry is not None,
            "cooldown_until": cd_expiry
        })
    return result


@app.post("/api/admin/models/add")
async def add_admin_model(payload: Dict[str, Any]):
    """Registers a new model in the catalog and logs audit trail."""
    platform = payload.get("platform")
    model_id = payload.get("model_id")
    if not platform or not model_id:
        return JSONResponse(status_code=400, content={"error": "platform and model_id are required"})

    display_name = payload.get("display_name") or model_id
    rpm_limit = payload.get("rpm_limit")
    rpd_limit = payload.get("rpd_limit")
    tpm_limit = payload.get("tpm_limit")
    tpd_limit = payload.get("tpd_limit")
    context_window = payload.get("context_window")
    base_score = payload.get("base_score", 3)
    shared_group = payload.get("shared_quota_group") or None

    await add_model(
        platform=platform,
        model_id=model_id,
        display_name=display_name,
        shared_quota_group=shared_group,
        rpm_limit=rpm_limit,
        rpd_limit=rpd_limit,
        tpm_limit=tpm_limit,
        tpd_limit=tpd_limit,
        context_window=context_window,
        base_score=base_score
    )
    await log_admin_audit("ADD_MODEL", "model", f"{platform}/{model_id}", f"Score: {base_score}, Limits: RPM={rpm_limit}, TPM={tpm_limit}")
    return {"status": "ok", "message": f"Added model {platform}/{model_id}"}


@app.post("/api/admin/models/toggle")
async def toggle_admin_model(payload: Dict[str, Any]):
    """Toggles model enabled state and logs audit trail."""
    platform = payload.get("platform")
    model_id = payload.get("model_id")
    enabled = bool(payload.get("enabled"))

    try:
        success = await toggle_model(platform, model_id, enabled)
        if not success:
            return JSONResponse(status_code=404, content={"error": f"Model {platform}/{model_id} not found."})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})

    await log_admin_audit("TOGGLE_MODEL", "model", f"{platform}/{model_id}", f"Enabled: {enabled}")
    return {"status": "ok", "enabled": enabled}


@app.post("/api/admin/models/update")
async def update_admin_model_endpoint(payload: Dict[str, Any]):
    """Updates model parameters and logs audit trail."""
    platform = payload.get("platform")
    model_id = payload.get("model_id")
    old_platform = payload.get("old_platform") or platform
    old_model_id = payload.get("old_model_id") or model_id

    await update_model(
        old_platform=old_platform,
        old_model_id=old_model_id,
        platform=platform,
        model_id=model_id,
        display_name=payload.get("display_name"),
        shared_quota_group=payload.get("shared_quota_group") or None,
        rpm_limit=payload.get("rpm_limit"),
        rpd_limit=payload.get("rpd_limit"),
        tpm_limit=payload.get("tpm_limit"),
        tpd_limit=payload.get("tpd_limit"),
        base_score=payload.get("base_score", 3)
    )
    await log_admin_audit("UPDATE_MODEL", "model", f"{platform}/{model_id}", f"Updated from {old_platform}/{old_model_id}")
    return {"status": "ok", "message": f"Updated model {platform}/{model_id}"}


@app.post("/api/admin/models/delete")
async def delete_admin_model_endpoint(payload: Dict[str, Any]):
    """Deletes a model from catalog and logs audit trail."""
    platform = payload.get("platform")
    model_id = payload.get("model_id")
    await delete_model(platform, model_id)
    await log_admin_audit("DELETE_MODEL", "model", f"{platform}/{model_id}", "Deleted from catalog")
    return {"status": "ok", "message": f"Deleted model {platform}/{model_id}"}


@app.post("/api/admin/models/clear-cooldown")
async def clear_model_cooldown_endpoint(payload: Dict[str, Any]):
    """Clears active rate-limit or daily cooldown for a model."""
    platform = payload.get("platform")
    model_id = payload.get("model_id")
    shared_quota_group = payload.get("shared_quota_group")
    if not platform or not model_id:
        return JSONResponse(status_code=400, content={"error": "platform and model_id are required"})

    await clear_cooldown(platform, model_id, shared_quota_group)
    await log_admin_audit("CLEAR_COOLDOWN", "model", f"{platform}/{model_id}", "Cleared cooldown quarantine")
    return {"status": "ok", "message": f"Cleared cooldown for {platform}/{model_id}"}


@app.get("/api/admin/keys")
async def get_admin_keys_endpoint():
    """Returns registered API keys status list."""
    return await get_admin_keys()


@app.post("/api/admin/keys/toggle")
async def toggle_admin_key(payload: Dict[str, Any]):
    """Toggles API key enabled state and logs audit trail."""
    platform = payload.get("platform")
    enabled = bool(payload.get("enabled"))
    await toggle_key(platform, enabled)
    await log_admin_audit("TOGGLE_KEY", "key", platform, f"Enabled: {enabled}")
    return {"status": "ok", "enabled": enabled}


@app.post("/api/admin/keys/add")
async def add_admin_key(payload: Dict[str, Any]):
    """Encrypts and registers or updates an API key in vault and logs audit trail."""
    platform = payload.get("platform")
    if not platform:
        return JSONResponse(status_code=400, content={"error": "platform is required"})

    display_name = payload.get("display_name")
    api_url = payload.get("api_url")
    raw_key = payload.get("api_key")

    await save_key(platform=platform, raw_key=raw_key, display_name=display_name, api_url=api_url)
    await log_admin_audit("SAVE_KEY", "key", platform, f"Display: {display_name}, URL: {api_url}")
    return {"status": "ok", "message": f"Key for platform '{platform}' saved."}


@app.post("/api/admin/keys/delete")
async def delete_admin_key_endpoint(payload: Dict[str, Any]):
    """Safely deletes an API key platform and disables associated models."""
    platform = payload.get("platform")
    await delete_key(platform)
    await log_admin_audit("DELETE_KEY", "key", platform, "Deleted provider key and disabled platform models")
    return {"status": "ok", "message": f"Deleted API key for platform '{platform}'"}


@app.get("/api/admin/triage")
async def get_admin_triage_settings():
    """Returns current triage classification strategy and model settings."""
    strategy = await get_setting("triage_strategy", default="llm")
    platform = await get_setting("triage_platform", default="google")
    model = await get_setting("triage_model", default="gemini-3.5-flash-lite")
    return {
        "triage_strategy": strategy,
        "triage_platform": platform,
        "triage_model": model
    }


@app.post("/api/admin/triage/save")
async def save_admin_triage_settings(payload: Dict[str, Any]):
    """Updates triage classification strategy and model settings."""
    strategy = payload.get("triage_strategy", "llm")
    platform = payload.get("triage_platform", "google")
    model = payload.get("triage_model", "gemini-3.5-flash-lite")

    await set_setting("triage_strategy", strategy)
    await set_setting("triage_platform", platform)
    await set_setting("triage_model", model)
    await log_admin_audit("SAVE_TRIAGE_SETTINGS", "setting", f"{platform}/{model}", f"Strategy: {strategy}, Model: {platform}/{model}")
    return {"status": "ok", "message": "Triage settings updated successfully"}


@app.get("/api/admin/analytics")
async def get_admin_analytics_endpoint(timeframe: str = "7d"):
    """Returns aggregated usage, token breakdown, performance latency, and per-model stats."""
    return await get_admin_analytics(timeframe)


@app.get("/api/debug/trace")
async def get_latest_debug_trace():
    """Returns the most recent multi-step request trace from database."""
    traces = await get_recent_traces(limit=1)
    if traces:
        return traces[0]
    return JSONResponse(status_code=404, content={"message": "No debug trace available yet. Send a request to generate a trace."})


@app.get("/api/admin/traces")
async def get_admin_traces_endpoint(limit: int = 20):
    """Returns recent request traces with triage difficulty, failover attempts, and token counts."""
    return await get_recent_traces(limit=limit)


@app.get("/api/admin/audit")
async def get_admin_audit_endpoint(limit: int = 20):
    """Returns recent administrative audit actions."""
    return await get_audit_logs(limit=limit)


@app.get("/api/admin/logs/stream")
async def stream_terminal_logs():
    """Streams live server terminal logs via SSE to web dashboard."""
    async def log_generator():
        # 1. Send recent log backlog
        backlog = list(recent_terminal_logs)
        for line in backlog:
            yield f"data: {json.dumps({'line': line})}\n\n"

        # 2. Subscribe to live stream
        queue: asyncio.Queue = asyncio.Queue()
        log_subscribers.add(queue)
        try:
            while True:
                msg = await queue.get()
                yield f"data: {json.dumps({'line': msg})}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            log_subscribers.discard(queue)

    return StreamingResponse(
        log_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )

