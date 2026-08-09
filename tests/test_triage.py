import pytest
import os
from src.db import init_db, get_db_connection
from src.triage import grade_difficulty_by_rule, grade_prompt_difficulty

TEST_DB = "test_pytest_triage.db"


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


def test_triage_heuristic_rules():
    simple_messages = [{"role": "user", "content": "What is 2 + 2?"}]
    score_simple = grade_difficulty_by_rule(simple_messages)
    assert score_simple == 1

    complex_messages = [
        {"role": "user", "content": "Please refactor and optimize this code:\n```python\nasync def run(): pass\n```\n" + "word " * 450}
    ]
    score_complex = grade_difficulty_by_rule(complex_messages)
    assert score_complex >= 4


@pytest.mark.asyncio
async def test_triage_fallback_on_missing_creds():
    await init_db(TEST_DB)
    simple_messages = [{"role": "user", "content": "Hello"}]
    async with get_db_connection(TEST_DB) as conn:
        fallback_score = await grade_prompt_difficulty(conn, simple_messages)
        assert fallback_score == 1


@pytest.mark.asyncio
async def test_triage_fallback_on_disabled_model():
    await init_db(TEST_DB)
    simple_messages = [{"role": "user", "content": "Hello"}]
    async with get_db_connection(TEST_DB) as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO api_keys (platform, display_name, encrypted_key, iv, status, enabled) VALUES ('google', 'Google AI', 'cipher', 'iv', 'healthy', 1)"
        )
        await conn.execute(
            "INSERT INTO models (platform, model_id, display_name, enabled) VALUES ('google', 'disabled-triage-model', 'Disabled Triage Model', 0)"
        )
        await conn.execute("INSERT INTO gateway_settings (key, value) VALUES ('triage_platform', 'google')")
        await conn.execute("INSERT INTO gateway_settings (key, value) VALUES ('triage_model', 'disabled-triage-model')")
        await conn.commit()

        fallback_score = await grade_prompt_difficulty(conn, simple_messages)
        assert fallback_score == 1
