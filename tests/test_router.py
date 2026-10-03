import pytest
import os
import src.config
from src.db import init_db, save_key, add_model, add_cooldown
from src.router import select_model_and_platform

import asyncio

TEST_DB = "test_pytest_router.db"


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
async def test_router_difficulty_matching_and_cooldown_failover():
    await save_key(platform="google", raw_key="sk-test-google")
    await save_key(platform="groq", raw_key="sk-test-groq")

    # High capability model (Score 5)
    await add_model(
        platform="google",
        model_id="gemini-pro",
        display_name="Gemini Pro",
        base_score=5,
        rpm_limit=100
    )
    # Lightweight model (Score 2)
    await add_model(
        platform="groq",
        model_id="llama-fast",
        display_name="Llama Fast",
        base_score=2,
        rpm_limit=100
    )

    # 1. High difficulty prompt (difficulty=5) picks Score 5 model
    route_hard = await select_model_and_platform("auto", difficulty=5)
    assert route_hard is not None
    assert route_hard["model_id"] == "gemini-pro"

    # 2. When gemini-pro is placed in cooldown, router falls back to groq
    await add_cooldown("google", "gemini-pro", base_seconds=60)
    route_fallback = await select_model_and_platform("auto", difficulty=5)
    assert route_fallback is not None
    assert route_fallback["model_id"] == "llama-fast"


@pytest.mark.asyncio
async def test_router_context_window_skipping():
    await save_key(platform="google", raw_key="sk-test-google")

    # Model with small context window (4000 tokens)
    await add_model(
        platform="google",
        model_id="small-ctx-model",
        display_name="Small Context Model",
        base_score=3,
        context_window=4000
    )

    # Prompt with 8000 tokens should be skipped
    route = await select_model_and_platform("auto", difficulty=3, estimated_prompt_tokens=8000)
    assert route is None

    # Prompt with 2000 tokens should be accepted
    route_ok = await select_model_and_platform("auto", difficulty=3, estimated_prompt_tokens=2000)
    assert route_ok is not None
    assert route_ok["model_id"] == "small-ctx-model"


@pytest.mark.asyncio
async def test_router_exclusion_list():
    await save_key(platform="google", raw_key="sk-test-google")
    await add_model(platform="google", model_id="model-a", base_score=4)
    await add_model(platform="google", model_id="model-b", base_score=3)

    # Exclude model-a
    route = await select_model_and_platform("auto", difficulty=4, exclude_models={("google", "model-a")})
    assert route is not None
    assert route["model_id"] == "model-b"


@pytest.mark.asyncio
async def test_router_pinned_direct_model():
    await save_key(platform="google", raw_key="sk-test-google")
    await add_model(platform="google", model_id="pinned-model", base_score=1)

    route = await select_model_and_platform(requested_model="pinned-model", difficulty=None)
    assert route is not None
    assert route["model_id"] == "pinned-model"
    assert route["platform"] == "google"
