import aiosqlite
from typing import Optional, Tuple
from src.config import logger


async def get_model_usage(
    conn: aiosqlite.Connection,
    model_id: str,
    quota_group: Optional[str] = None
) -> Tuple[int, int, int, int]:
    """
    Queries request and token counts over sliding 1-minute and 24-hour windows.
    Aggregates usage across all models sharing the same quota_group if present.
    Returns: (rpm, rpd, tpm, tpd)
    """
    cursor = await conn.execute(
        """
        SELECT 
            COUNT(*) AS total_requests, 
            COALESCE(SUM(tokens_input + tokens_output), 0) AS total_tokens 
        FROM usage_log 
        WHERE (
            model_id = ? 
            OR model_id IN (
                SELECT model_id 
                FROM models 
                WHERE shared_quota_group = ? AND shared_quota_group IS NOT NULL
            )
        ) 
        AND timestamp >= datetime('now', '-1 minute');
        """,
        (model_id, quota_group)
    )
    row_minute = await cursor.fetchone()
    rpm, tpm = (row_minute[0] or 0, row_minute[1] or 0) if row_minute else (0, 0)

    cursor = await conn.execute(
        """
        SELECT 
            COUNT(*) AS total_requests, 
            COALESCE(SUM(tokens_input + tokens_output), 0) AS total_tokens 
        FROM usage_log 
        WHERE (
            model_id = ? 
            OR model_id IN (
                SELECT model_id 
                FROM models 
                WHERE shared_quota_group = ? AND shared_quota_group IS NOT NULL
            )
        ) 
        AND timestamp >= datetime('now', '-24 hours');
        """,
        (model_id, quota_group)
    )
    row_day = await cursor.fetchone()
    rpd, tpd = (row_day[0] or 0, row_day[1] or 0) if row_day else (0, 0)

    logger.debug(f"[QUOTA] Usage for '{model_id}' (group='{quota_group}'): RPM={rpm}, RPD={rpd}, TPM={tpm}, TPD={tpd}")
    return (rpm, rpd, tpm, tpd)


async def get_avg_model_latency(
    conn: aiosqlite.Connection,
    model_id: str,
    quota_group: Optional[str] = None
) -> float:
    """
    Queries rolling average duration_ms of the last 10 successful requests and converts to seconds.
    Returns: float (seconds)
    """
    cursor = await conn.execute(
        """
        SELECT COALESCE(AVG(duration_ms), 0.0) 
        FROM (
            SELECT duration_ms 
            FROM usage_log 
            WHERE (
                model_id = ? 
                OR model_id IN (
                    SELECT model_id 
                    FROM models 
                    WHERE shared_quota_group = ? AND shared_quota_group IS NOT NULL
                )
            )
            AND request_success = 1
            ORDER BY timestamp DESC 
            LIMIT 10
        );
        """,
        (model_id, quota_group)
    )
    row = await cursor.fetchone()
    avg_ms = row[0] if row else 0.0
    avg_sec = (avg_ms or 0.0) / 1000.0
    logger.debug(f"[QUOTA LATENCY] Rolling avg latency for '{model_id}': {avg_sec:.3f}s")
    return avg_sec
