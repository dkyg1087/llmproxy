from typing import Optional, Dict, Any, List
from src.db.connection import get_db_connection


async def get_candidate_models(requested_model: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Returns eligible, healthy models with active credentials that are not quarantined.
    If requested_model is specified (pinned route), returns only that model if eligible.
    """
    query = """
        SELECT 
            m.model_id, 
            m.platform, 
            a.id AS key_id,
            m.display_name,
            m.shared_quota_group,
            m.rpm_limit, 
            m.rpd_limit, 
            m.tpm_limit, 
            m.tpd_limit, 
            m.context_window,
            m.base_score
        FROM models m
        JOIN api_keys a ON m.platform = a.platform
        WHERE m.enabled = 1
          AND m.is_healthy = 1
          AND a.enabled = 1
          AND a.is_healthy = 1
          AND NOT EXISTS (
              SELECT 1 
              FROM rate_limit_cooldowns c 
              WHERE c.platform = m.platform 
                AND c.model_id = m.model_id 
                AND c.expires_at > datetime('now')
          )
    """
    params = []
    if requested_model and requested_model != "auto":
        query += " AND m.model_id = ?"
        params.append(requested_model)

    query += " ORDER BY m.base_score DESC"

    async with get_db_connection() as conn:
        cur = await conn.execute(query, tuple(params))
        rows = await cur.fetchall()
        return [
            {
                "model_id": r[0],
                "platform": r[1],
                "key_id": r[2],
                "display_name": r[3],
                "shared_quota_group": r[4],
                "rpm_limit": r[5],
                "rpd_limit": r[6],
                "tpm_limit": r[7],
                "tpd_limit": r[8],
                "context_window": r[9],
                "base_score": r[10]
            }
            for r in rows
        ]


async def get_admin_models() -> List[Dict[str, Any]]:
    """Returns full catalog model configurations for dashboard display."""
    async with get_db_connection() as conn:
        cur = await conn.execute(
            """
            SELECT platform, model_id, display_name, shared_quota_group,
                   rpm_limit, rpd_limit, tpm_limit, tpd_limit,
                   context_window, base_score, is_healthy, enabled
            FROM models
            ORDER BY platform, base_score DESC
            """
        )
        rows = await cur.fetchall()
        return [
            {
                "platform": r[0],
                "model_id": r[1],
                "display_name": r[2],
                "shared_quota_group": r[3],
                "rpm_limit": r[4],
                "rpd_limit": r[5],
                "tpm_limit": r[6],
                "tpd_limit": r[7],
                "context_window": r[8],
                "base_score": r[9],
                "is_healthy": r[10] == 1,
                "enabled": r[11] == 1
            }
            for r in rows
        ]


async def add_model(
    platform: str,
    model_id: str,
    display_name: Optional[str] = None,
    shared_quota_group: Optional[str] = None,
    rpm_limit: Optional[int] = None,
    rpd_limit: Optional[int] = None,
    tpm_limit: Optional[int] = None,
    tpd_limit: Optional[int] = None,
    context_window: Optional[int] = None,
    base_score: int = 3
) -> None:
    """Registers a new model in catalog, or updates limits if already exists."""
    display_name = display_name or model_id
    async with get_db_connection() as conn:
        await conn.execute(
            """
            INSERT INTO models (
                platform, model_id, display_name, shared_quota_group,
                rpm_limit, rpd_limit, tpm_limit, tpd_limit,
                context_window, base_score, is_healthy, enabled
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1)
            ON CONFLICT(platform, model_id) DO UPDATE SET
                display_name = excluded.display_name,
                shared_quota_group = excluded.shared_quota_group,
                rpm_limit = excluded.rpm_limit,
                rpd_limit = excluded.rpd_limit,
                tpm_limit = excluded.tpm_limit,
                tpd_limit = excluded.tpd_limit,
                context_window = excluded.context_window,
                base_score = excluded.base_score,
                is_healthy = 1,
                enabled = 1
            """,
            (platform, model_id, display_name, shared_quota_group, rpm_limit, rpd_limit, tpm_limit, tpd_limit, context_window, base_score)
        )
        await conn.commit()


async def update_model(
    platform: str,
    model_id: str,
    old_platform: Optional[str] = None,
    old_model_id: Optional[str] = None,
    display_name: Optional[str] = None,
    shared_quota_group: Optional[str] = None,
    rpm_limit: Optional[int] = None,
    rpd_limit: Optional[int] = None,
    tpm_limit: Optional[int] = None,
    tpd_limit: Optional[int] = None,
    context_window: Optional[int] = None,
    base_score: int = 3
) -> None:
    """Updates model parameters and cascades ID changes to usage logs and cooldowns."""
    old_platform = old_platform or platform
    old_model_id = old_model_id or model_id
    display_name = display_name or model_id

    async with get_db_connection() as conn:
        if (old_platform, old_model_id) != (platform, model_id):
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
                display_name = ?,
                shared_quota_group = ?,
                rpm_limit = ?,
                rpd_limit = ?,
                tpm_limit = ?,
                tpd_limit = ?,
                context_window = ?,
                base_score = ?
            WHERE platform = ? AND model_id = ?
            """,
            (platform, model_id, display_name, shared_quota_group, rpm_limit, rpd_limit, tpm_limit, tpd_limit, context_window, base_score, old_platform, old_model_id)
        )
        await conn.commit()


async def toggle_model(platform: str, model_id: str, enabled: bool) -> bool:
    """Toggles model enabled state. Rejects enabling if no active API key exists for platform."""
    async with get_db_connection() as conn:
        if enabled:
            cur_key = await conn.execute(
                "SELECT 1 FROM api_keys WHERE platform = ? AND enabled = 1", 
                (platform,)
            )
            if not await cur_key.fetchone():
                raise ValueError(f"Cannot enable model '{model_id}': no active API key found for platform '{platform}'.")

        cur = await conn.execute(
            "UPDATE models SET enabled = ? WHERE platform = ? AND model_id = ?",
            (1 if enabled else 0, platform, model_id)
        )
        await conn.commit()
        return cur.rowcount > 0


async def set_model_health(platform: str, model_id: str, is_healthy: bool) -> bool:
    """Updates model health state (automated gateway health marking)."""
    async with get_db_connection() as conn:
        cur = await conn.execute(
            "UPDATE models SET is_healthy = ? WHERE platform = ? AND model_id = ?",
            (1 if is_healthy else 0, platform, model_id)
        )
        await conn.commit()
        return cur.rowcount > 0


async def delete_model(platform: str, model_id: str) -> bool:
    """Deletes a model from catalog and cleans up any active cooldowns."""
    async with get_db_connection() as conn:
        await conn.execute("DELETE FROM rate_limit_cooldowns WHERE platform = ? AND model_id = ?", (platform, model_id))
        cur = await conn.execute("DELETE FROM models WHERE platform = ? AND model_id = ?", (platform, model_id))
        await conn.commit()
        return cur.rowcount > 0


async def update_model_limits(
    platform: str,
    model_id: str,
    rpm_limit: Optional[int] = None,
    rpd_limit: Optional[int] = None,
    tpm_limit: Optional[int] = None,
    tpd_limit: Optional[int] = None,
    context_window: Optional[int] = None
) -> bool:
    """Dynamically updates rate limits and/or context window for a given model."""
    async with get_db_connection() as conn:
        cur = await conn.execute(
            """
            UPDATE models 
            SET rpm_limit = COALESCE(?, rpm_limit),
                rpd_limit = COALESCE(?, rpd_limit),
                tpm_limit = COALESCE(?, tpm_limit),
                tpd_limit = COALESCE(?, tpd_limit),
                context_window = COALESCE(?, context_window)
            WHERE platform = ? AND model_id = ?
            """,
            (rpm_limit, rpd_limit, tpm_limit, tpd_limit, context_window, platform, model_id)
        )
        await conn.commit()
        return cur.rowcount > 0
