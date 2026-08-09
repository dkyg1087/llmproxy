import pytest
import os
from src.db import init_db, get_db_connection, add_cooldown
from src.key_vault import encrypt_key
from src.router import select_model_and_platform

TEST_DB = "test_pytest_router.db"


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
async def test_bwk_router_selection_and_failover():
    await init_db(TEST_DB)
    async with get_db_connection(TEST_DB) as conn:
        cipher, iv = encrypt_key("sk-test")
        cur = await conn.execute(
            "INSERT OR REPLACE INTO api_keys (platform, display_name, encrypted_key, iv, status, enabled) VALUES (?, ?, ?, ?, 'healthy', 1)",
            ("google", "Google AI", cipher, iv)
        )
        k1 = cur.lastrowid

        cur = await conn.execute(
            "INSERT INTO api_keys (platform, display_name, encrypted_key, iv) VALUES (?, ?, ?, ?)",
            ("groq", "Groq Inc", cipher, iv)
        )
        k2 = cur.lastrowid

        await conn.execute(
            "INSERT INTO models (platform, model_id, display_name, base_score) VALUES (?, ?, ?, ?)",
            ("google", "gemini-3-flash", "Gemini Flash", 70)
        )
        await conn.execute(
            "INSERT INTO models (platform, model_id, display_name, base_score) VALUES (?, ?, ?, ?)",
            ("groq", "gemma-4-31b", "Gemma 31B", 50)
        )
        await conn.commit()

        # 1. Higher score model wins initially
        route1 = await select_model_and_platform(conn, "auto", 1)
        assert route1 is not None
        assert route1["model_id"] == "gemini-3-flash"

        # 2. When Gemini enters cooldown, router falls back to Groq
        await add_cooldown(conn, "google", "gemini-3-flash", k1, 60)
        route2 = await select_model_and_platform(conn, "auto", 1)
        assert route2 is not None
        assert route2["model_id"] == "gemma-4-31b"


@pytest.mark.asyncio
async def test_prompt_token_capacity_rejection():
    await init_db(TEST_DB)
    async with get_db_connection(TEST_DB) as conn:
        cipher, iv = encrypt_key("sk-test")
        cur = await conn.execute(
            "INSERT INTO api_keys (platform, display_name, encrypted_key, iv) VALUES (?, ?, ?, ?)",
            ("groq", "Groq Inc", cipher, iv)
        )
        k1 = cur.lastrowid

        # Model with 8k TPM limit
        await conn.execute(
            "INSERT INTO models (platform, model_id, display_name, base_score, tpm_limit) VALUES (?, ?, ?, ?, ?)",
            ("groq", "small-model", "Small Model", 90, 8000)
        )
        await conn.commit()

        # Prompt with 85k tokens exceeds 8k TPM limit
        route = await select_model_and_platform(conn, "auto", 1, estimated_prompt_tokens=85000)
        assert route is None


@pytest.mark.asyncio
async def test_score_zero_emergency_fallback():
    await init_db(TEST_DB)
    async with get_db_connection(TEST_DB) as conn:
        cipher, iv = encrypt_key("sk-test")
        cur = await conn.execute(
            "INSERT OR REPLACE INTO api_keys (platform, display_name, encrypted_key, iv, status, enabled) VALUES (?, ?, ?, ?, 'healthy', 1)",
            ("google", "Google AI", cipher, iv)
        )
        k1 = cur.lastrowid

        await conn.execute("DELETE FROM models")
        # Primary model (score 4) and Emergency Fallback model (score 0)
        await conn.execute(
            "INSERT INTO models (platform, model_id, display_name, base_score) VALUES (?, ?, ?, ?)",
            ("google", "primary-model", "Primary Model", 4)
        )
        await conn.execute(
            "INSERT INTO models (platform, model_id, display_name, base_score) VALUES (?, ?, ?, ?)",
            ("google", "fallback-model", "Fallback Model", 0)
        )
        await conn.commit()

        # 1. Primary model is selected while healthy
        route1 = await select_model_and_platform(conn, "auto", 1)
        assert route1 is not None
        assert route1["model_id"] == "primary-model"

        # 2. When primary model enters cooldown, emergency score 0 model is activated
        await add_cooldown(conn, "google", "primary-model", k1, 60)
        route2 = await select_model_and_platform(conn, "auto", 1)
        assert route2 is not None
        assert route2["model_id"] == "fallback-model"


def test_extract_quota_limits():
    from src.providers.openai_compat import OpenAIAdapter
    adapter = OpenAIAdapter()

    # General headers test
    headers = {
        "x-ratelimit-limit-requests": "10000",
        "x-ratelimit-limit-requests-day": "50000",
        "x-ratelimit-limit-tokens": "2000000"
    }
    limits = adapter.extract_quota_limits("openai", 200, headers)
    assert limits["rpm_limit"] == 10000
    assert limits["rpd_limit"] == 50000
    assert limits["tpm_limit"] == 2000000
