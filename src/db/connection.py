import aiosqlite
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional
import src.config
from src.config import DB_PATH, logger

# database schema
INIT_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;

-- 1. api_keys: Stores encrypted platform credentials
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    api_url TEXT,
    encrypted_key TEXT NOT NULL,
    iv TEXT NOT NULL,
    is_healthy INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_used_at DATETIME
);

-- 2. models: Model catalog with quotas, context windows, and base scores
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
    context_window INTEGER,
    base_score INTEGER DEFAULT 3,
    is_healthy INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    UNIQUE(platform, model_id)
);

-- 3. usage_log: Sliding-window token and latency tracking
CREATE TABLE IF NOT EXISTS usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    model_id TEXT NOT NULL,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    tokens_input INTEGER NOT NULL,
    tokens_output INTEGER NOT NULL,
    ttft_ms INTEGER,
    duration_ms INTEGER NOT NULL,
    request_success INTEGER NOT NULL DEFAULT 1,
    error_type TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_model_time ON usage_log(model_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_usage_latency ON usage_log(model_id, request_success, timestamp);

-- 4. rate_limit_cooldowns: Active model quarantines (per platform/model)
CREATE TABLE IF NOT EXISTS rate_limit_cooldowns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    model_id TEXT NOT NULL,
    expires_at DATETIME NOT NULL,
    UNIQUE(platform, model_id)
);

-- 5. gateway_settings: Key-value system configuration
CREATE TABLE IF NOT EXISTS gateway_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 6. admin_audit_log: Audit trail of dashboard actions
CREATE TABLE IF NOT EXISTS admin_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    details TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 7. request_traces: Detailed failover diagnostics & empty response tracking
CREATE TABLE IF NOT EXISTS request_traces (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id        TEXT NOT NULL,
    triage_difficulty INTEGER,
    has_tools         INTEGER NOT NULL DEFAULT 0,
    final_platform    TEXT,
    final_model_id    TEXT,
    final_status      INTEGER NOT NULL,
    finish_reason     TEXT,
    tokens_output     INTEGER DEFAULT 0,
    response_preview  TEXT,
    attempts_detail   TEXT NOT NULL,
    error_summary     TEXT,
    created_at        DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_traces_created ON request_traces(created_at);
"""


# Connection manager
@asynccontextmanager
async def get_db_connection(db_path: Optional[str] = None) -> AsyncGenerator[aiosqlite.Connection, None]:
    """
    Internal async context manager yielding an SQLite connection.
    """
    target = db_path or src.config.DB_PATH
    conn = await aiosqlite.connect(target)
    await conn.execute("PRAGMA journal_mode=WAL;")
    await conn.execute("PRAGMA busy_timeout=5000;")
    await conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
    finally:
        await conn.close()


# Initialization

async def init_db(db_path: Optional[str] = None) -> None:
    """
    Bootstraps schema and indexes, and runs startup auto-pruning.
    """
    target = db_path or src.config.DB_PATH
    logger.info(f"[DB INIT] Initializing database at {target}...")
    async with get_db_connection(target) as conn:
        await conn.executescript(INIT_SCHEMA_SQL)
        
        
        # Clear expired rate limits
        await conn.execute("DELETE FROM rate_limit_cooldowns WHERE expires_at <= datetime('now')")
        # Prune usage_log older than 30 days
        await conn.execute("DELETE FROM usage_log WHERE timestamp < datetime('now', '-30 days')")
        # Retain only the most recent 200 request traces
        await conn.execute(
            """
            DELETE FROM request_traces 
            WHERE id NOT IN (SELECT id FROM request_traces ORDER BY id DESC LIMIT 200)
            """
        )
        await conn.commit()
    logger.info("[DB INIT] Database initialization and auto-pruning complete.")
