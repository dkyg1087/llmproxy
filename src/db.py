import aiosqlite
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any, List
from src.config import DB_PATH, logger
from src.key_vault import decrypt_key

INIT_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;

-- 1. schema_info: Tracks database migration state
CREATE TABLE IF NOT EXISTS schema_info (
    version INTEGER PRIMARY KEY
);

-- 2. api_keys: Stores encrypted platform credentials (one key per platform)
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    api_url TEXT,
    encrypted_key TEXT NOT NULL,
    iv TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('healthy', 'cooldown', 'error', 'disabled', 'unknown')) DEFAULT 'healthy',
    enabled INTEGER DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_used_at DATETIME
);

-- 3. models: The available model catalog with quota parameters
CREATE TABLE IF NOT EXISTS models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    model_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    shared_quota_group TEXT,
    rpm_limit INTEGER,
    rpd_limit INTEGER,
    tpm_limit INTEGER,
    tpd_limit INTEGER,
    context_window INTEGER DEFAULT 8192,
    base_score INTEGER DEFAULT 3,
    enabled INTEGER DEFAULT 1,
    UNIQUE(platform, model_id),
    FOREIGN KEY(platform) REFERENCES api_keys(platform)
);

-- 4. key_capabilities: Tracks model authorization per key
CREATE TABLE IF NOT EXISTS key_capabilities (
    key_id INTEGER NOT NULL,
    model_id TEXT NOT NULL,
    is_capable INTEGER NOT NULL DEFAULT 1,
    last_tested DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (key_id, model_id),
    FOREIGN KEY(key_id) REFERENCES api_keys(id) ON DELETE CASCADE
);

-- 5. usage_log: Sliding-window token and latency tracking
CREATE TABLE IF NOT EXISTS usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    model_id TEXT NOT NULL,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    tokens_input INTEGER NOT NULL,
    tokens_output INTEGER NOT NULL,
    ttft_ms INTEGER,
    duration_ms INTEGER NOT NULL,
    request_success INTEGER DEFAULT 1,
    FOREIGN KEY(key_id) REFERENCES api_keys(id)
);

-- 6. rate_limit_cooldowns: Stores key/model quarantines to survive rate limits
CREATE TABLE IF NOT EXISTS rate_limit_cooldowns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    model_id TEXT NOT NULL,
    key_id INTEGER NOT NULL,
    expires_at DATETIME NOT NULL,
    UNIQUE(platform, model_id, key_id),
    FOREIGN KEY(key_id) REFERENCES api_keys(id)
);

-- 7. gateway_settings: Key-value settings table
CREATE TABLE IF NOT EXISTS gateway_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 8. admin_audit_log: Audit trail of dashboard configuration changes
CREATE TABLE IF NOT EXISTS admin_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    details TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


@asynccontextmanager
async def get_db_connection(db_path: str = DB_PATH):
    """
    Async context manager yielding a configured SQLite connection (WAL mode, foreign keys, busy timeout).
    Guarantees connection closure on block exit.
    """
    conn = await aiosqlite.connect(db_path)
    await conn.execute("PRAGMA journal_mode=WAL;")
    await conn.execute("PRAGMA busy_timeout=5000;")
    await conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
    finally:
        await conn.close()


async def init_db(db_path: str = DB_PATH):
    """Initializes the database schema and populates default disabled placeholders if empty."""
    logger.info(f"[DB INIT] Initializing database at {db_path} in WAL mode...")
    async with get_db_connection(db_path) as conn:
        await conn.executescript(INIT_SCHEMA_SQL)
        
        cur = await conn.execute("SELECT COUNT(*) FROM api_keys")
        count = (await cur.fetchone())[0] or 0
        if count == 0:
            logger.info("[DB INIT] Fresh database detected. Inserting disabled Google API key placeholder...")
            await conn.execute(
                """
                INSERT INTO api_keys (platform, display_name, api_url, encrypted_key, iv, status, enabled)
                VALUES ('google', 'Google AI Studio', 'https://generativelanguage.googleapis.com/v1beta', '', '', 'disabled', 0)
                """
            )
            await conn.execute(
                """
                INSERT INTO models (platform, model_id, display_name, shared_quota_group, rpm_limit, rpd_limit, tpm_limit, base_score, enabled)
                VALUES 
                    ('google', 'gemini-2.5-flash', 'Gemini 2.5 Flash', NULL, 5, 1500, 1000000, 4, 1),
                    ('google', 'gemini-3.1-flash-lite', 'Gemini 3.1 Flash Lite', NULL, 15, 1500, 1000000, 2, 1)
                """
            )
        await conn.commit()
    logger.info("[DB INIT] Database schema initialization complete.")


async def log_admin_audit(conn: aiosqlite.Connection, action: str, target_type: str, target_id: str, details: str = ""):
    """Logs an administrative change to both the system logger and database audit table."""
    await conn.execute(
        "INSERT INTO admin_audit_log (action, target_type, target_id, details) VALUES (?, ?, ?, ?)",
        (action, target_type, target_id, details)
    )
    logger.info(f"[ADMIN AUDIT] {action.upper()} {target_type.upper()} '{target_id}' | {details}")


async def get_api_keys(conn: aiosqlite.Connection, platform: Optional[str] = None) -> Any:
    """Returns all enabled API keys, or a decrypted key dictionary if platform is specified."""
    if platform:
        return await get_decrypted_key(conn, platform)
    cur = await conn.execute("SELECT id, platform, display_name, api_url, status, enabled, last_used_at FROM api_keys WHERE enabled = 1")
    rows = await cur.fetchall()
    return [
        {
            "id": r[0],
            "platform": r[1],
            "display_name": r[2],
            "api_url": r[3],
            "status": r[4],
            "enabled": r[5] == 1,
            "last_used_at": r[6]
        }
        for r in rows
    ]


async def get_decrypted_key(conn: aiosqlite.Connection, platform: str) -> Optional[Dict[str, Any]]:
    """Retrieves and decrypts the active API key for a given platform."""
    cur = await conn.execute(
        "SELECT id, platform, display_name, api_url, encrypted_key, iv, status FROM api_keys WHERE platform = ? AND enabled = 1",
        (platform,)
    )
    row = await cur.fetchone()
    if not row:
        return None

    key_id, platform_name, display_name, api_url, cipher, iv, status = row
    decrypted = decrypt_key(cipher, iv)
    return {
        "id": key_id,
        "platform": platform_name,
        "display_name": display_name,
        "api_url": api_url,
        "api_key": decrypted,
        "status": status
    }


async def disable_key(conn: aiosqlite.Connection, key_id: int):
    """Disables an API key in case of non-recoverable error (e.g. invalid key)."""
    await conn.execute("UPDATE api_keys SET status = 'error', enabled = 0 WHERE id = ?", (key_id,))
    await conn.commit()


async def mark_model_incapable(conn: aiosqlite.Connection, key_id: int, model_id: str):
    """Marks a model incapable for a key (e.g. 404 Model Not Found)."""
    await conn.execute("UPDATE key_capabilities SET is_capable = 0 WHERE key_id = ? AND model_id = ?", (key_id, model_id))
    await conn.commit()


async def get_setting(conn: aiosqlite.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    """Retrieves a setting value from gateway_settings."""
    cur = await conn.execute("SELECT value FROM gateway_settings WHERE key = ?", (key,))
    row = await cur.fetchone()
    return row[0] if row else default


async def set_setting(conn: aiosqlite.Connection, key: str, value: str):
    """Sets a setting value in gateway_settings."""
    await conn.execute(
        "INSERT INTO gateway_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value)
    )
    await conn.commit()


async def add_cooldown(conn: aiosqlite.Connection, platform: str, model_id: str, key_id: int, duration_seconds: int = 60, shared_quota_group: Optional[str] = None):
    """Quarantines a model/key or entire shared quota group upon rate limit (HTTP 429)."""
    expires_at = f"datetime('now', '+{duration_seconds} seconds')"
    if shared_quota_group:
        cur = await conn.execute("SELECT platform, model_id FROM models WHERE shared_quota_group = ?", (shared_quota_group,))
        group_models = await cur.fetchall()
        for p, m in group_models:
            await conn.execute(
                f"""
                INSERT INTO rate_limit_cooldowns (platform, model_id, key_id, expires_at)
                VALUES (?, ?, ?, {expires_at})
                ON CONFLICT(platform, model_id, key_id) DO UPDATE SET expires_at = excluded.expires_at
                """,
                (p, m, key_id)
            )
    else:
        await conn.execute(
            f"""
            INSERT INTO rate_limit_cooldowns (platform, model_id, key_id, expires_at)
            VALUES (?, ?, ?, {expires_at})
            ON CONFLICT(platform, model_id, key_id) DO UPDATE SET expires_at = excluded.expires_at
            """,
            (platform, model_id, key_id)
        )
    await conn.commit()


async def is_cooldown_active(conn: aiosqlite.Connection, platform: str, model_id: str, key_id: int) -> bool:
    """Checks whether an active rate limit cooldown exists."""
    cur = await conn.execute(
        "SELECT 1 FROM rate_limit_cooldowns WHERE platform = ? AND model_id = ? AND key_id = ? AND expires_at > datetime('now')",
        (platform, model_id, key_id)
    )
    row = await cur.fetchone()
    return row is not None


async def get_models_catalog(conn: aiosqlite.Connection) -> List[Dict[str, Any]]:
    """Returns catalog models."""
    cur = await conn.execute("SELECT platform, model_id, display_name, shared_quota_group, rpm_limit, rpd_limit, tpm_limit, tpd_limit, base_score, enabled FROM models WHERE enabled = 1")
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
            "base_score": r[8],
            "enabled": r[9] == 1
        }
        for r in rows
    ]


async def update_model_limits(
    conn: aiosqlite.Connection, 
    platform: str, 
    model_id: str, 
    rpm_limit: Optional[int] = None,
    rpd_limit: Optional[int] = None,
    tpm_limit: Optional[int] = None,
    tpd_limit: Optional[int] = None
):
    """Updates RPM, RPD, TPM, and TPD limits for a given model dynamically."""
    await conn.execute(
        """
        UPDATE models 
        SET rpm_limit = COALESCE(?, rpm_limit),
            rpd_limit = COALESCE(?, rpd_limit),
            tpm_limit = COALESCE(?, tpm_limit),
            tpd_limit = COALESCE(?, tpd_limit)
        WHERE platform = ? AND model_id = ?
        """,
        (rpm_limit, rpd_limit, tpm_limit, tpd_limit, platform, model_id)
    )
    await conn.commit()


async def log_request_usage(
    conn: aiosqlite.Connection,
    key_id: int,
    platform: str,
    model_id: str,
    tokens_input: int,
    tokens_output: int,
    duration_ms: int = 0,
    ttft_ms: Optional[int] = None,
    request_success: bool = True
):
    """Logs request usage and updates last_used_at."""
    await conn.execute(
        """
        INSERT INTO usage_log (key_id, platform, model_id, tokens_input, tokens_output, ttft_ms, duration_ms, request_success)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (key_id, platform, model_id, tokens_input, tokens_output, ttft_ms, duration_ms, 1 if request_success else 0)
    )
    await conn.execute("UPDATE api_keys SET last_used_at = datetime('now') WHERE id = ?", (key_id,))
    await conn.commit()