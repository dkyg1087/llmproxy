import pytest
import os
import src.config
from src.db import (
    init_db,
    get_db_connection,
    get_setting,
    set_setting,
    get_all_settings,
    delete_setting,
    save_key,
    get_decrypted_key,
    get_admin_keys,
    toggle_key,
    delete_key,
    add_model,
    update_model,
    toggle_model,
    delete_model,
    get_admin_models,
    log_request_usage,
    get_model_usage,
    get_model_ms_per_token,
    add_cooldown,
    clear_cooldown,
    log_admin_audit,
    get_audit_logs,
    log_request_trace,
    get_recent_traces,
    get_admin_analytics,
)

import asyncio

TEST_DB = "test_pytest_db.db"


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch):
    monkeypatch.setattr(src.config, "DB_PATH", TEST_DB)
    if os.path.exists(TEST_DB):
        try:
            os.remove(TEST_DB)
        except OSError:
            pass
    asyncio.run(init_db(TEST_DB))
    yield
    if os.path.exists(TEST_DB):
        try:
            os.remove(TEST_DB)
        except OSError:
            pass


@pytest.mark.asyncio
async def test_db_settings_crud():
    await set_setting("test_key", "test_val")
    val = await get_setting("test_key")
    assert val == "test_val"

    all_s = await get_all_settings()
    assert all_s.get("test_key") == "test_val"

    deleted = await delete_setting("test_key")
    assert deleted is True
    assert await get_setting("test_key") is None


@pytest.mark.asyncio
async def test_db_keys_and_models_crud():
    # 1. Save and retrieve key
    await save_key(platform="google", raw_key="sk-test-google-123", display_name="Google AI", api_url="https://api.google.com")
    creds = await get_decrypted_key("google")
    assert creds is not None
    assert creds["api_key"] == "sk-test-google-123"
    assert creds["display_name"] == "Google AI"

    admin_keys = await get_admin_keys()
    assert len(admin_keys) >= 1
    assert admin_keys[0]["platform"] == "google"
    assert admin_keys[0]["enabled"] is True

    # 2. Add model under valid key
    await add_model(
        platform="google",
        model_id="gemini-flash",
        display_name="Gemini Flash",
        shared_quota_group="google_shared",
        rpm_limit=15,
        base_score=4,
        context_window=1000000
    )
    models = await get_admin_models()
    assert len(models) == 1
    assert models[0]["model_id"] == "gemini-flash"
    assert models[0]["context_window"] == 1000000

    # 3. Update model
    await update_model(
        platform="google",
        model_id="gemini-flash-v2",
        old_platform="google",
        old_model_id="gemini-flash",
        display_name="Gemini Flash V2",
        base_score=5
    )
    models_updated = await get_admin_models()
    assert models_updated[0]["model_id"] == "gemini-flash-v2"
    assert models_updated[0]["base_score"] == 5

    # 4. Toggle model
    await toggle_model("google", "gemini-flash-v2", False)
    assert (await get_admin_models())[0]["enabled"] is False

    # 5. Model toggle rejection when key is missing
    with pytest.raises(ValueError, match="no active API key"):
        await toggle_model("unknown_platform", "some_model", True)

    # 6. Delete model
    deleted = await delete_model("google", "gemini-flash-v2")
    assert deleted is True
    assert len(await get_admin_models()) == 0

    # 7. Delete key
    key_deleted = await delete_key("google")
    assert key_deleted is True
    assert await get_decrypted_key("google") is None


@pytest.mark.asyncio
async def test_db_usage_cooldowns_and_analytics():
    await save_key(platform="groq", raw_key="sk-groq-key")
    await add_model(platform="groq", model_id="llama-3.3", shared_quota_group="groq_free", rpm_limit=30)

    # 1. Log request usage (zero-conn signature with error_type and TTFT)
    await log_request_usage(
        platform="groq",
        model_id="llama-3.3",
        tokens_input=120,
        tokens_output=80,
        duration_ms=400,
        ttft_ms=150,
        request_success=True
    )

    rpm, rpd, tpm, tpd = await get_model_usage("llama-3.3", "groq_free")
    assert rpm == 1
    assert rpd == 1
    assert tpm == 200
    assert tpd == 200

    # 2. Latency calculation (duration_ms / tokens_output)
    ms_per_token = await get_model_ms_per_token("llama-3.3", "groq")
    assert ms_per_token == 400.0 / 80.0  # 5.0 ms/token

    # 3. Rate limit cooldown
    await add_cooldown("groq", "llama-3.3", base_seconds=60)
    async with get_db_connection(TEST_DB) as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM rate_limit_cooldowns WHERE platform = 'groq' AND model_id = 'llama-3.3'")
        assert (await cur.fetchone())[0] == 1

    # 3b. Fixed cooldown (12h) and clear cooldown
    await add_cooldown("groq", "llama-3.3", fixed_seconds=43200)
    async with get_db_connection(TEST_DB) as conn:
        cur = await conn.execute("SELECT expires_at FROM rate_limit_cooldowns WHERE platform = 'groq' AND model_id = 'llama-3.3'")
        row = await cur.fetchone()
        assert row is not None

    await clear_cooldown("groq", "llama-3.3")
    async with get_db_connection(TEST_DB) as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM rate_limit_cooldowns WHERE platform = 'groq' AND model_id = 'llama-3.3'")
        assert (await cur.fetchone())[0] == 0

    # 3c. Exclude client errors from get_model_usage
    await log_request_usage("groq", "llama-3.3", 0, 0, duration_ms=10, request_success=False, error_type="auth_error")
    await log_request_usage("groq", "llama-3.3", 0, 0, duration_ms=10, request_success=False, error_type="context_overflow")
    await log_request_usage("groq", "llama-3.3", 0, 0, duration_ms=10, request_success=False, error_type="rate_limit")
    rpm_after, _, _, _ = await get_model_usage("llama-3.3", "groq_free")
    # Initial success was 1, plus rate_limit (1) = 2. auth_error and context_overflow are excluded!
    assert rpm_after == 2

    # 4. Analytics aggregation
    analytics = await get_admin_analytics("today")
    assert analytics["total_requests"] == 4
    assert analytics["successful_requests"] == 1
    assert analytics["total_tokens"] == 200
    assert analytics["avg_ttft_ms"] == 150


@pytest.mark.asyncio
async def test_db_audit_and_traces():
    # 1. Admin audit log
    await log_admin_audit("TEST_ACTION", "model", "test-model", "Audit test details")
    audits = await get_audit_logs(limit=10)
    assert len(audits) >= 1
    assert audits[0]["action"] == "TEST_ACTION"
    assert audits[0]["target_id"] == "test-model"

    # 2. Request traces
    await log_request_trace(
        request_id="req-test-trace-1",
        final_status=200,
        attempts_detail='[{"attempt": 1, "model": "gemini-3.5", "status": 200}]',
        triage_difficulty=2,
        has_tools=False,
        final_platform="google",
        final_model_id="gemini-3.5",
        finish_reason="stop",
        tokens_output=42,
        response_preview="Hello world",
        error_summary=None
    )
    traces = await get_recent_traces(limit=5)
    assert len(traces) == 1
    assert traces[0]["request_id"] == "req-test-trace-1"
    assert traces[0]["triage_difficulty"] == 2
    assert traces[0]["final_status"] == 200
