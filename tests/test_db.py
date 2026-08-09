import pytest
import os
from src.db import (
    init_db,
    get_db_connection,
    get_api_keys,
    log_request_usage,
    add_cooldown,
    update_model_limits,
)
from src.key_vault import encrypt_key

TEST_DB = "test_pytest_db.db"


@pytest.fixture(autouse=True)
def cleanup_test_db():
    if os.path.exists(TEST_DB):
        try:
            os.remove(TEST_DB)
        except OSError:
            pass
    yield
    if os.path.exists(TEST_DB):
        try:
            os.remove(TEST_DB)
        except OSError:
            pass


@pytest.mark.asyncio
async def test_db_init_and_crud():
    await init_db(TEST_DB)
    async with get_db_connection(TEST_DB) as conn:
        cipher, iv = encrypt_key("sk-google-key")
        cur = await conn.execute(
            "INSERT OR REPLACE INTO api_keys (platform, display_name, encrypted_key, iv, status, enabled) VALUES (?, ?, ?, ?, 'healthy', 1)",
            ("google", "Google AI", cipher, iv)
        )
        key_id = cur.lastrowid
        await conn.execute(
            "INSERT INTO models (platform, model_id, display_name, shared_quota_group) VALUES (?, ?, ?, ?)",
            ("google", "gemini-3-flash", "Gemini Flash", "google_free")
        )
        await conn.execute(
            "INSERT INTO models (platform, model_id, display_name, shared_quota_group) VALUES (?, ?, ?, ?)",
            ("google", "gemini-3-lite", "Gemini Lite", "google_free")
        )
        await conn.commit()

        # Test key retrieval & decryption
        creds = await get_api_keys(conn, "google")
        assert creds is not None
        assert creds["api_key"] == "sk-google-key"

        # Test metric logging
        await log_request_usage(conn, key_id, "google", "gemini-3-flash", 100, 200, 500, request_success=True)
        cur = await conn.execute("SELECT COUNT(*) FROM usage_log")
        row = await cur.fetchone()
        assert row[0] == 1

        # Test group cooldown propagation
        await add_cooldown(conn, "google", "gemini-3-flash", key_id, 60, "google_free")
        cur = await conn.execute("SELECT COUNT(*) FROM rate_limit_cooldowns")
        row = await cur.fetchone()
        assert row[0] == 2

        # Test dynamic limit auto-tuning
        await update_model_limits(conn, "google", "gemini-3-flash", rpm_limit=15, tpm_limit=40000)
        cur = await conn.execute("SELECT rpm_limit, tpm_limit FROM models WHERE model_id = 'gemini-3-flash'")
        row = await cur.fetchone()
        assert row[0] == 15 and row[1] == 40000
