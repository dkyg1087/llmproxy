from typing import Optional, Tuple
from src.db.connection import get_db_connection


async def log_request_usage(
    platform: str,
    model_id: str,
    tokens_input: int,
    tokens_output: int,
    duration_ms: int = 0,
    ttft_ms: Optional[int] = None,
    request_success: bool = True,
    error_type: Optional[str] = None
) -> None:
    """Logs request token and latency metrics, updating platform last_used_at timestamp."""
    async with get_db_connection() as conn:
        await conn.execute(
            """
            INSERT INTO usage_log (
                platform, model_id, tokens_input, tokens_output,
                ttft_ms, duration_ms, request_success, error_type
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                platform, model_id, tokens_input, tokens_output,
                ttft_ms, duration_ms, 1 if request_success else 0, error_type
            )
        )
        await conn.execute(
            "UPDATE api_keys SET last_used_at = datetime('now') WHERE platform = ?",
            (platform,)
        )
        await conn.commit()


async def add_cooldown(
    platform: str,
    model_id: str,
    base_seconds: int = 30,
    max_seconds: int = 300,
    fixed_seconds: Optional[int] = None,
    shared_quota_group: Optional[str] = None
) -> int:
    """
    Quarantines a model with progressive backoff or a fixed duration (e.g. 12h for daily quota).
    Returns applied quarantine duration in seconds.
    """
    async with get_db_connection() as conn:
        if fixed_seconds is not None and fixed_seconds > 0:
            duration = fixed_seconds
        else:
            cur_strikes = await conn.execute(
                """
                SELECT COUNT(*) FROM usage_log 
                WHERE platform = ? AND model_id = ? 
                  AND error_type IN ('rate_limit', 'overloaded')
                  AND timestamp >= datetime('now', '-10 minutes')
                """,
                (platform, model_id)
            )
            strike_count = (await cur_strikes.fetchone())[0] or 0
            duration = min(max_seconds, base_seconds * (2 ** strike_count))

        expires_at_sql = f"datetime('now', '+{duration} seconds')"

        targets = [(platform, model_id)]
        if shared_quota_group:
            cur_group = await conn.execute(
                "SELECT platform, model_id FROM models WHERE shared_quota_group = ?",
                (shared_quota_group,)
            )
            targets = await cur_group.fetchall()

        for p, m in targets:
            await conn.execute(
                f"""
                INSERT INTO rate_limit_cooldowns (platform, model_id, expires_at)
                VALUES (?, ?, {expires_at_sql})
                ON CONFLICT(platform, model_id) DO UPDATE SET expires_at = excluded.expires_at
                """,
                (p, m)
            )
        await conn.commit()
        return duration


async def clear_cooldown(
    platform: str,
    model_id: str,
    shared_quota_group: Optional[str] = None
) -> bool:
    """Removes active cooldowns for a model or shared quota group."""
    async with get_db_connection() as conn:
        targets = [(platform, model_id)]
        if shared_quota_group:
            cur_group = await conn.execute(
                "SELECT platform, model_id FROM models WHERE shared_quota_group = ?",
                (shared_quota_group,)
            )
            targets = await cur_group.fetchall()

        for p, m in targets:
            await conn.execute(
                "DELETE FROM rate_limit_cooldowns WHERE platform = ? AND model_id = ?",
                (p, m)
            )
        await conn.commit()
        return True


async def get_model_usage(
    model_id: str,
    quota_group: Optional[str] = None
) -> Tuple[int, int, int, int]:
    """
    Queries request and token counts over sliding 1-minute and 24-hour windows.
    Filters out client configuration errors (auth_error, model_not_found, context_overflow).
    Returns: (rpm, rpd, tpm, tpd)
    """
    async with get_db_connection() as conn:
        cur_min = await conn.execute(
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
            AND (error_type IS NULL OR error_type NOT IN ('auth_error', 'model_not_found', 'context_overflow'))
            AND timestamp >= datetime('now', '-1 minute');
            """,
            (model_id, quota_group)
        )
        row_min = await cur_min.fetchone()
        rpm, tpm = (row_min[0] or 0, row_min[1] or 0) if row_min else (0, 0)

        cur_day = await conn.execute(
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
            AND (error_type IS NULL OR error_type NOT IN ('auth_error', 'model_not_found', 'context_overflow'))
            AND timestamp >= datetime('now', '-24 hours');
            """,
            (model_id, quota_group)
        )
        row_day = await cur_day.fetchone()
        rpd, tpd = (row_day[0] or 0, row_day[1] or 0) if row_day else (0, 0)

        return (rpm, rpd, tpm, tpd)



async def get_model_ms_per_token(
    model_id: str,
    platform: Optional[str] = None
) -> float:
    """Queries rolling average ms/token over the last 10 successful requests for this specific model."""
    async with get_db_connection() as conn:
        if platform:
            query = """
                SELECT COALESCE(AVG(duration_ms * 1.0 / MAX(1, tokens_output)), 0.0)
                FROM (
                    SELECT duration_ms, tokens_output
                    FROM usage_log
                    WHERE model_id = ? AND platform = ? AND request_success = 1
                    ORDER BY timestamp DESC
                    LIMIT 10
                );
            """
            params = (model_id, platform)
        else:
            query = """
                SELECT COALESCE(AVG(duration_ms * 1.0 / MAX(1, tokens_output)), 0.0)
                FROM (
                    SELECT duration_ms, tokens_output
                    FROM usage_log
                    WHERE model_id = ? AND request_success = 1
                    ORDER BY timestamp DESC
                    LIMIT 10
                );
            """
            params = (model_id,)

        cur = await conn.execute(query, params)
        row = await cur.fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0

