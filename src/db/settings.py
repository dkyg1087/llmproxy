from typing import Optional, Dict
from src.db.connection import get_db_connection


async def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """Retrieves a configuration value from gateway_settings."""
    async with get_db_connection() as conn:
        cur = await conn.execute("SELECT value FROM gateway_settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row[0] if row else default


async def set_setting(key: str, value: str) -> None:
    """Sets or updates a configuration value in gateway_settings."""
    async with get_db_connection() as conn:
        await conn.execute(
            "INSERT INTO gateway_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value)
        )
        await conn.commit()


async def get_all_settings() -> Dict[str, str]:
    """Retrieves all key-value configuration pairs from gateway_settings."""
    async with get_db_connection() as conn:
        cur = await conn.execute("SELECT key, value FROM gateway_settings")
        rows = await cur.fetchall()
        return {row[0]: row[1] for row in rows}


async def delete_setting(key: str) -> bool:
    """Deletes a setting from gateway_settings. Returns True if deleted."""
    async with get_db_connection() as conn:
        cur = await conn.execute("DELETE FROM gateway_settings WHERE key = ?", (key,))
        await conn.commit()
        return cur.rowcount > 0
