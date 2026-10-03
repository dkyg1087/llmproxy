from typing import Optional, Dict, Any, List
from src.config import logger
from src.db.connection import get_db_connection


# ── 1. REQUEST TRACES (DIAGNOSTICS & FAILOVER) ───────────────────────────────

async def log_request_trace(
    request_id: str,
    final_status: int,
    attempts_detail: str,
    triage_difficulty: Optional[int] = None,
    has_tools: bool = False,
    final_platform: Optional[str] = None,
    final_model_id: Optional[str] = None,
    finish_reason: Optional[str] = None,
    tokens_output: int = 0,
    response_preview: Optional[str] = None,
    error_summary: Optional[str] = None
) -> None:
    """Records diagnostic routing trace and multi-step failover history."""
    async with get_db_connection() as conn:
        await conn.execute(
            """
            INSERT INTO request_traces (
                request_id, triage_difficulty, has_tools,
                final_platform, final_model_id, final_status,
                finish_reason, tokens_output, response_preview,
                attempts_detail, error_summary
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id, triage_difficulty, 1 if has_tools else 0,
                final_platform, final_model_id, final_status,
                finish_reason, tokens_output, response_preview,
                attempts_detail, error_summary
            )
        )
        await conn.commit()


async def get_recent_traces(limit: int = 50) -> List[Dict[str, Any]]:
    """Retrieves recent request traces for diagnostics and debugging."""
    async with get_db_connection() as conn:
        cur = await conn.execute(
            """
            SELECT id, request_id, triage_difficulty, has_tools,
                   final_platform, final_model_id, final_status,
                   finish_reason, tokens_output, response_preview,
                   attempts_detail, error_summary, created_at
            FROM request_traces
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,)
        )
        rows = await cur.fetchall()
        return [
            {
                "id": r[0],
                "request_id": r[1],
                "triage_difficulty": r[2],
                "has_tools": r[3] == 1,
                "final_platform": r[4],
                "final_model_id": r[5],
                "final_status": r[6],
                "finish_reason": r[7],
                "tokens_output": r[8],
                "response_preview": r[9],
                "attempts_detail": r[10],
                "error_summary": r[11],
                "created_at": r[12]
            }
            for r in rows
        ]


# ── 2. ADMIN AUDIT TRAIL ─────────────────────────────────────────────────────

async def log_admin_audit(
    action: str,
    target_type: str,
    target_id: str,
    details: str = ""
) -> None:
    """Logs an administrative change to both the system logger and audit table."""
    logger.info(f"[ADMIN AUDIT] {action.upper()} {target_type.upper()} '{target_id}' | {details}")
    async with get_db_connection() as conn:
        await conn.execute(
            "INSERT INTO admin_audit_log (action, target_type, target_id, details) VALUES (?, ?, ?, ?)",
            (action, target_type, target_id, details)
        )
        await conn.commit()


async def get_audit_logs(limit: int = 50) -> List[Dict[str, Any]]:
    """Returns recent dashboard administrative audit actions."""
    async with get_db_connection() as conn:
        cur = await conn.execute(
            """
            SELECT id, action, target_type, target_id, details, created_at
            FROM admin_audit_log
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,)
        )
        rows = await cur.fetchall()
        return [
            {
                "id": r[0],
                "action": r[1],
                "target_type": r[2],
                "target_id": r[3],
                "details": r[4],
                "created_at": r[5]
            }
            for r in rows
        ]


# ── 3. ANALYTICS REPORTING ───────────────────────────────────────────────────

async def get_admin_analytics(timeframe: str = "7d") -> Dict[str, Any]:
    """Returns aggregated usage summary and per-model breakdown."""
    time_filter_sql = ""
    if timeframe == "today":
        time_filter_sql = "WHERE timestamp >= datetime('now', 'start of day')"
    elif timeframe == "7d":
        time_filter_sql = "WHERE timestamp >= datetime('now', '-7 days')"
    elif timeframe == "30d":
        time_filter_sql = "WHERE timestamp >= datetime('now', '-30 days')"

    async with get_db_connection() as conn:
        cur = await conn.execute(
            f"""
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
        )
        summary = await cur.fetchone()
        total_reqs = summary[0] or 0
        success_reqs = summary[1] or 0
        failed_reqs = summary[2] or 0
        in_tokens = summary[3] or 0
        out_tokens = summary[4] or 0
        avg_latency = round(summary[5] or 0)
        avg_ttft = round(summary[6] or 0)
        success_rate = round((success_reqs / total_reqs * 100), 1) if total_reqs > 0 else 100.0

        cur_m = await conn.execute(
            f"""
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
        )
        m_rows = await cur_m.fetchall()
        models_breakdown = [
            {
                "platform": r[0],
                "model_id": r[1],
                "request_count": r[2] or 0,
                "total_tokens": r[4] or 0,
                "avg_latency_ms": round(r[5] or 0),
                "success_rate": round(((r[3] or 0) / (r[2] or 1) * 100), 1)
            }
            for r in m_rows
        ]

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
