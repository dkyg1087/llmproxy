import time
import os
import json
from typing import Dict, Any, List, Optional
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from src.config import PORT, logger
from src.models import ChatCompletionRequest
from src.db import init_db, get_db_connection, log_admin_audit, get_setting, set_setting
from src.key_vault import encrypt_key
from src.triage import grade_prompt_difficulty
from src.proxy import execute_proxy_request
from src.router.quota import get_model_usage


@asynccontextmanager
async def lifespan(app: FastAPI):
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
        async with get_db_connection() as conn:
            difficulty = await grade_prompt_difficulty(conn, payload.messages)

        triage_content = {
            "difficulty": difficulty,
            "system_prompt_reference": "1=casual, 2=editing, 3=multi-step, 4=reasoning/summarization, 5=complex-system-design"
        }
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
        async with get_db_connection() as conn:
            difficulty = await grade_prompt_difficulty(conn, payload.messages)
        
        logger.info(f"[MAIN ROUTE] Prompt difficulty graded as Difficulty={difficulty}. Delegating to failover proxy...")
        return await execute_proxy_request(payload, difficulty=difficulty)

    else:
        logger.info(f"[MAIN ROUTE] Pinned direct route requested for model='{payload.model}'. Delegating to failover proxy...")
        return await execute_proxy_request(payload, difficulty=None)


@app.get("/v1/models")
async def get_models():
    """
    Exposes only the virtual 'auto' model in the public model catalog list.
    """
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
        cur = await conn.execute("SELECT COUNT(*), COALESCE(SUM(tokens_input + tokens_output), 0), COALESCE(AVG(ttft_ms), 0) FROM usage_log")
        row = await cur.fetchone()
        total_requests, total_tokens, avg_ttft_ms = row[0] or 0, row[1] or 0, round(row[2] or 0, 1)

        cur_cd = await conn.execute("SELECT COUNT(DISTINCT model_id) FROM rate_limit_cooldowns WHERE expires_at > datetime('now')")
        active_cooldowns = (await cur_cd.fetchone())[0] or 0

        cur_models = await conn.execute(
            """
            SELECT m.platform, m.model_id, m.shared_quota_group, m.rpm_limit, m.rpd_limit, m.tpm_limit, m.tpd_limit, m.enabled
            FROM models m JOIN api_keys a ON m.platform = a.platform
            WHERE m.enabled = 1 AND a.enabled = 1 AND a.status != 'error'
            """
        )
        model_rows = await cur_models.fetchall()

        total_capacity_rpm = 0
        current_used_rpm = 0
        total_capacity_rpd = 0
        current_used_rpd = 0

        total_capacity_tpm = 0
        current_used_tpm = 0
        total_capacity_tpd = 0
        current_used_tpd = 0
        healthy_models = 0

        cur_cd_set = await conn.execute("SELECT DISTINCT model_id FROM rate_limit_cooldowns WHERE expires_at > datetime('now')")
        cooldown_set = set(r[0] for r in await cur_cd_set.fetchall())

        seen_groups = set()
        for p, m_id, group, rpm_limit, rpd_limit, tpm_limit, tpd_limit, enabled in model_rows:
            if m_id not in cooldown_set:
                healthy_models += 1

            group_key = group or f"{p}:{m_id}"
            if group_key not in seen_groups:
                seen_groups.add(group_key)
                rpm, rpd, tpm, tpd = await get_model_usage(conn, m_id, group)
                if rpm_limit:
                    total_capacity_rpm += rpm_limit
                    current_used_rpm += rpm
                if rpd_limit:
                    total_capacity_rpd += rpd_limit
                    current_used_rpd += rpd
                if tpm_limit:
                    total_capacity_tpm += tpm_limit
                    current_used_tpm += tpm
                if tpd_limit:
                    total_capacity_tpd += tpd_limit
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
async def get_admin_models():
    """Returns full catalog model details including RPM, RPD, TPM, and TPD quotas."""
    async with get_db_connection() as conn:
        cur = await conn.execute(
            """
            SELECT m.platform, m.model_id, m.display_name, m.shared_quota_group,
                   m.rpm_limit, m.rpd_limit, m.tpm_limit, m.tpd_limit,
                   m.base_score, m.enabled
            FROM models m
            ORDER BY m.platform, m.base_score DESC
            """
        )
        rows = await cur.fetchall()

        cur_cd = await conn.execute("SELECT DISTINCT model_id FROM rate_limit_cooldowns WHERE expires_at > datetime('now')")
        cooldown_models = set(r[0] for r in await cur_cd.fetchall())

        result = []
        for r in rows:
            platform, model_id, display_name, shared_group, rpm_limit, rpd_limit, tpm_limit, tpd_limit, base_score, enabled = r
            rpm, rpd, tpm, tpd = await get_model_usage(conn, model_id, shared_group)
            result.append({
                "platform": platform,
                "model_id": model_id,
                "display_name": display_name,
                "shared_quota_group": shared_group,
                "rpm_limit": rpm_limit,
                "rpd_limit": rpd_limit,
                "tpm_limit": tpm_limit,
                "tpd_limit": tpd_limit,
                "base_score": base_score,
                "enabled": enabled == 1,
                "current_rpm": rpm,
                "current_rpd": rpd,
                "current_tpm": tpm,
                "current_tpd": tpd,
                "is_cooldown": model_id in cooldown_models
            })
        return result


@app.post("/api/admin/models/add")
async def add_admin_model(payload: Dict[str, Any]):
    """Registers a new model in the catalog and logs audit trail."""
    platform = payload.get("platform")
    model_id = payload.get("model_id")
    display_name = payload.get("display_name") or model_id
    shared_group = payload.get("shared_quota_group") or None
    rpm_limit = payload.get("rpm_limit")
    rpd_limit = payload.get("rpd_limit")
    tpm_limit = payload.get("tpm_limit")
    tpd_limit = payload.get("tpd_limit")
    base_score = payload.get("base_score", 3)

    if not platform or not model_id:
        return JSONResponse(status_code=400, content={"error": "platform and model_id are required"})

    async with get_db_connection() as conn:
        await conn.execute(
            """
            INSERT INTO models (platform, model_id, display_name, shared_quota_group, rpm_limit, rpd_limit, tpm_limit, tpd_limit, base_score, enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(platform, model_id) DO UPDATE SET
                display_name = excluded.display_name,
                shared_quota_group = excluded.shared_quota_group,
                rpm_limit = excluded.rpm_limit,
                rpd_limit = excluded.rpd_limit,
                tpm_limit = excluded.tpm_limit,
                tpd_limit = excluded.tpd_limit,
                base_score = excluded.base_score,
                enabled = 1
            """,
            (platform, model_id, display_name, shared_group, rpm_limit, rpd_limit, tpm_limit, tpd_limit, base_score)
        )

        cur_keys = await conn.execute("SELECT id FROM api_keys WHERE platform = ?", (platform,))
        key_rows = await cur_keys.fetchall()
        for k in key_rows:
            await conn.execute(
                """
                INSERT INTO key_capabilities (key_id, model_id, is_capable)
                VALUES (?, ?, 1)
                ON CONFLICT(key_id, model_id) DO UPDATE SET is_capable = 1
                """,
                (k[0], model_id)
            )

        await log_admin_audit(conn, "ADD_MODEL", "model", f"{platform}/{model_id}", f"Name: {display_name}, Score: {base_score}, RPM: {rpm_limit}, TPM: {tpm_limit}")
        await conn.commit()

    return {"status": "ok", "message": f"Added model {platform}/{model_id}"}


@app.post("/api/admin/models/toggle")
async def toggle_admin_model(payload: Dict[str, Any]):
    """Toggles model enabled state and logs audit trail."""
    platform = payload.get("platform")
    model_id = payload.get("model_id")
    enabled = 1 if payload.get("enabled") else 0

    async with get_db_connection() as conn:
        await conn.execute("UPDATE models SET enabled = ? WHERE platform = ? AND model_id = ?", (enabled, platform, model_id))
        await log_admin_audit(conn, "TOGGLE_MODEL", "model", f"{platform}/{model_id}", f"Enabled: {enabled == 1}")
        await conn.commit()
    return {"status": "ok", "enabled": enabled}


@app.post("/api/admin/models/update")
async def update_admin_model(payload: Dict[str, Any]):
    """Updates model parameters (including platform and model_id) and logs audit trail."""
    platform = payload.get("platform")
    model_id = payload.get("model_id")
    old_platform = payload.get("old_platform") or platform
    old_model_id = payload.get("old_model_id") or model_id
    display_name = payload.get("display_name")
    shared_group = payload.get("shared_quota_group") or None
    rpm_limit = payload.get("rpm_limit")
    rpd_limit = payload.get("rpd_limit")
    tpm_limit = payload.get("tpm_limit")
    tpd_limit = payload.get("tpd_limit")
    base_score = payload.get("base_score", 3)

    async with get_db_connection() as conn:
        if (old_platform, old_model_id) != (platform, model_id):
            await conn.execute(
                "UPDATE key_capabilities SET model_id = ? WHERE model_id = ?",
                (model_id, old_model_id)
            )
            await conn.execute(
                "UPDATE usage_log SET platform = ?, model_id = ? WHERE platform = ? AND model_id = ?",
                (platform, model_id, old_platform, old_model_id)
            )
            await conn.execute(
                "UPDATE rate_limit_cooldowns SET platform = ?, model_id = ? WHERE platform = ? AND model_id = ?",
                (platform, model_id, old_platform, old_model_id)
            )

        await conn.execute(
            """
            UPDATE models 
            SET platform = ?,
                model_id = ?,
                display_name = COALESCE(?, display_name),
                shared_quota_group = ?,
                rpm_limit = ?,
                rpd_limit = ?,
                tpm_limit = ?,
                tpd_limit = ?,
                base_score = ?
            WHERE platform = ? AND model_id = ?
            """,
            (platform, model_id, display_name, shared_group, rpm_limit, rpd_limit, tpm_limit, tpd_limit, base_score, old_platform, old_model_id)
        )
        await log_admin_audit(conn, "UPDATE_MODEL", "model", f"{platform}/{model_id}", f"Renamed from {old_platform}/{old_model_id}, Name: {display_name}, Score: {base_score}")
        await conn.commit()
    return {"status": "ok", "message": f"Updated model {platform}/{model_id}"}


@app.post("/api/admin/models/delete")
async def delete_admin_model(payload: Dict[str, Any]):
    """Deletes a model from catalog and logs audit trail."""
    platform = payload.get("platform")
    model_id = payload.get("model_id")

    async with get_db_connection() as conn:
        await conn.execute("DELETE FROM models WHERE platform = ? AND model_id = ?", (platform, model_id))
        await conn.execute("DELETE FROM key_capabilities WHERE model_id = ?", (model_id,))
        await log_admin_audit(conn, "DELETE_MODEL", "model", f"{platform}/{model_id}", "Deleted from catalog")
        await conn.commit()
    return {"status": "ok", "message": f"Deleted model {platform}/{model_id}"}


@app.get("/api/admin/keys")
async def get_admin_keys():
    """Returns registered API keys status list."""
    async with get_db_connection() as conn:
        cur = await conn.execute("SELECT platform, display_name, api_url, status, enabled, last_used_at FROM api_keys ORDER BY id ASC")
        rows = await cur.fetchall()
        return [
            {
                "platform": r[0],
                "display_name": r[1],
                "api_url": r[2],
                "status": r[3],
                "enabled": r[4] == 1,
                "last_used_at": r[5]
            }
            for r in rows
        ]


@app.post("/api/admin/keys/toggle")
async def toggle_admin_key(payload: Dict[str, Any]):
    """Toggles API key enabled state and logs audit trail."""
    platform = payload.get("platform")
    enabled = 1 if payload.get("enabled") else 0

    async with get_db_connection() as conn:
        await conn.execute("UPDATE api_keys SET enabled = ? WHERE platform = ?", (enabled, platform))
        await log_admin_audit(conn, "TOGGLE_KEY", "key", platform, f"Enabled: {enabled == 1}")
        await conn.commit()
    return {"status": "ok", "enabled": enabled}


@app.post("/api/admin/keys/add")
async def add_admin_key(payload: Dict[str, Any]):
    """Encrypts and registers a new or updated API key and logs audit trail."""
    platform = payload.get("platform")
    display_name = payload.get("display_name")
    api_url = payload.get("api_url")
    raw_key = payload.get("api_key")

    if not platform:
        return JSONResponse(status_code=400, content={"error": "platform is required"})

    async with get_db_connection() as conn:
        if raw_key:
            cipher, iv = encrypt_key(raw_key)
            await conn.execute(
                """
                INSERT INTO api_keys (platform, display_name, api_url, encrypted_key, iv, status, enabled)
                VALUES (?, ?, ?, ?, ?, 'healthy', 1)
                ON CONFLICT(platform) DO UPDATE SET
                    display_name = excluded.display_name,
                    api_url = excluded.api_url,
                    encrypted_key = excluded.encrypted_key,
                    iv = excluded.iv,
                    status = 'healthy',
                    enabled = 1
                """,
                (platform, display_name, api_url, cipher, iv)
            )
        else:
            await conn.execute(
                """
                UPDATE api_keys
                SET display_name = COALESCE(?, display_name),
                    api_url = COALESCE(?, api_url)
                WHERE platform = ?
                """,
                (display_name, api_url, platform)
            )

        cur_key = await conn.execute("SELECT id FROM api_keys WHERE platform = ?", (platform,))
        row = await cur_key.fetchone()
        if row:
            key_id = row[0]
            cur_models = await conn.execute("SELECT model_id FROM models WHERE platform = ?", (platform,))
            m_rows = await cur_models.fetchall()
            for (m_id,) in m_rows:
                await conn.execute(
                    """
                    INSERT INTO key_capabilities (key_id, model_id, is_capable)
                    VALUES (?, ?, 1)
                    ON CONFLICT(key_id, model_id) DO UPDATE SET is_capable = 1
                    """,
                    (key_id, m_id)
                )

        await log_admin_audit(conn, "SAVE_KEY", "key", platform, f"Display: {display_name}, URL: {api_url}")
        await conn.commit()

    return {"status": "ok", "message": f"Key for platform '{platform}' saved."}


@app.post("/api/admin/keys/delete")
async def delete_admin_key(payload: Dict[str, Any]):
    """Deletes an API key platform and logs audit trail."""
    platform = payload.get("platform")

    async with get_db_connection() as conn:
        cur_key = await conn.execute("SELECT id FROM api_keys WHERE platform = ?", (platform,))
        row = await cur_key.fetchone()
        if row:
            await conn.execute("DELETE FROM key_capabilities WHERE key_id = ?", (row[0],))
        await conn.execute("DELETE FROM api_keys WHERE platform = ?", (platform,))
        await log_admin_audit(conn, "DELETE_KEY", "key", platform, "Deleted provider key")
        await conn.commit()
    return {"status": "ok", "message": f"Deleted API key for platform '{platform}'"}


@app.get("/api/admin/triage")
async def get_admin_triage_settings():
    """Returns current triage classification strategy and model settings."""
    async with get_db_connection() as conn:
        strategy = await get_setting(conn, "triage_strategy", default="llm")
        platform = await get_setting(conn, "triage_platform", default="google")
        model = await get_setting(conn, "triage_model", default="gemini-3.1-flash-lite")
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
    model = payload.get("triage_model", "gemini-3.1-flash-lite")

    async with get_db_connection() as conn:
        await set_setting(conn, "triage_strategy", strategy)
        await set_setting(conn, "triage_platform", platform)
        await set_setting(conn, "triage_model", model)
        await log_admin_audit(conn, "SAVE_TRIAGE_SETTINGS", "setting", f"{platform}/{model}", f"Strategy: {strategy}, Model: {platform}/{model}")
        await conn.commit()

    return {"status": "ok", "message": "Triage settings updated successfully"}


@app.get("/api/admin/analytics")
async def get_admin_analytics(timeframe: str = "7d"):
    """Returns aggregated usage, token breakdown, performance latency, and per-model stats."""
    time_filter_sql = ""
    if timeframe == "today":
        time_filter_sql = "WHERE timestamp >= datetime('now', 'start of day')"
    elif timeframe == "7d":
        time_filter_sql = "WHERE timestamp >= datetime('now', '-7 days')"
    elif timeframe == "30d":
        time_filter_sql = "WHERE timestamp >= datetime('now', '-30 days')"

    async with get_db_connection() as conn:
        summary_query = f"""
            SELECT 
                COUNT(*) AS total_requests,
                SUM(CASE WHEN request_success = 1 THEN 1 ELSE 0 END) AS successful_requests,
                SUM(CASE WHEN request_success = 0 THEN 1 ELSE 0 END) AS failed_requests,
                COALESCE(SUM(tokens_input), 0) AS total_input_tokens,
                COALESCE(SUM(tokens_output), 0) AS total_output_tokens,
                COALESCE(AVG(duration_ms), 0) AS avg_latency_ms,
                COALESCE(AVG(ttft_ms), 0) AS avg_ttft_ms
            FROM usage_log
            {time_filter_sql}
        """
        cur = await conn.execute(summary_query)
        summary_row = await cur.fetchone()

        total_reqs = summary_row[0] or 0
        success_reqs = summary_row[1] or 0
        failed_reqs = summary_row[2] or 0
        in_tokens = summary_row[3] or 0
        out_tokens = summary_row[4] or 0
        avg_latency = round(summary_row[5] or 0)
        avg_ttft = round(summary_row[6] or 0)
        success_rate = round((success_reqs / total_reqs * 100), 1) if total_reqs > 0 else 100.0

        model_query = f"""
            SELECT 
                platform,
                model_id,
                COUNT(*) AS request_count,
                SUM(CASE WHEN request_success = 1 THEN 1 ELSE 0 END) AS success_count,
                COALESCE(SUM(tokens_input + tokens_output), 0) AS total_tokens,
                COALESCE(AVG(duration_ms), 0) AS avg_latency_ms
            FROM usage_log
            {time_filter_sql}
            GROUP BY platform, model_id
            ORDER BY total_tokens DESC, request_count DESC
        """
        cur_m = await conn.execute(model_query)
        m_rows = await cur_m.fetchall()

        models_breakdown = []
        for r in m_rows:
            m_reqs = r[2] or 0
            m_success = r[3] or 0
            m_rate = round((m_success / m_reqs * 100), 1) if m_reqs > 0 else 100.0
            models_breakdown.append({
                "platform": r[0],
                "model_id": r[1],
                "request_count": m_reqs,
                "total_tokens": r[4] or 0,
                "avg_latency_ms": round(r[5] or 0),
                "success_rate": m_rate
            })

        return {
            "timeframe": timeframe,
            "total_requests": total_reqs,
            "successful_requests": success_reqs,
            "failed_requests": failed_reqs,
            "success_rate": success_rate,
            "total_input_tokens": in_tokens,
            "total_output_tokens": out_tokens,
            "total_tokens": in_tokens + out_tokens,
            "avg_latency_ms": avg_latency,
            "avg_ttft_ms": avg_ttft,
            "models_breakdown": models_breakdown
        }


@app.get("/api/debug/trace")
async def get_latest_debug_trace():
    """Returns the JSON trace of the last proxy request for debugging payload transformations."""
    scratch_trace = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scratch", "latest_debug_trace.json")
    if os.path.exists(scratch_trace):
        try:
            with open(scratch_trace, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": f"Failed to read trace file: {e}"})
    return JSONResponse(status_code=404, content={"message": "No debug trace available yet. Send a request to generate a trace."})
