import pytest
import os
import src.config
from src.db import init_db, set_setting
from src.triage import (
    grade_difficulty_by_rule,
    sanitize_messages_for_triage,
    grade_prompt_difficulty,
)

import asyncio

TEST_DB = "test_pytest_triage.db"


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


def test_triage_heuristic_rules():
    # 1. Simple short casual query
    assert grade_difficulty_by_rule([{"role": "user", "content": "Hi"}]) == 1

    # 2. Code fences -> floor of 4
    code_msg = [{"role": "user", "content": "Look at this:\n```python\nprint(1)\n```"}]
    assert grade_difficulty_by_rule(code_msg) >= 4

    # 3. Multi-step numbered instructions
    steps_msg = [{"role": "user", "content": "Please do:\n1. First step\n2. Second step\n3. Third step"}]
    assert grade_difficulty_by_rule(steps_msg) >= 3

    # 4. Tool calls present -> floor of 3
    tool_msg = [{"role": "assistant", "content": None, "tool_calls": [{"id": "t1", "function": {"name": "search"}}]}]
    assert grade_difficulty_by_rule(tool_msg) >= 3


def test_sanitize_messages_for_triage():
    long_tool_output = "x" * 500
    raw_messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Search for info."},
        {"role": "tool", "content": long_tool_output},
        {"role": "user", "content": "What did you find?"},
    ]

    sanitized = sanitize_messages_for_triage(raw_messages, max_recent_turns=4)

    # Tool output should be truncated to 250 characters + indicator
    tool_entry = next((m for m in sanitized if "tool: " in m["content"]), None)
    if tool_entry:
        assert len(tool_entry["content"]) < 350
        assert "[truncated]" in tool_entry["content"]

    # Consecutive messages of same role should be merged
    roles = [m["role"] for m in sanitized]
    for i in range(len(roles) - 1):
        assert roles[i] != roles[i + 1]


@pytest.mark.asyncio
async def test_grade_prompt_difficulty_heuristic_mode():
    await set_setting("triage_strategy", "heuristic")
    difficulty = await grade_prompt_difficulty([{"role": "user", "content": "Quick question"}])
    assert difficulty == 1


@pytest.mark.asyncio
async def test_grade_prompt_difficulty_fallback_to_rule():
    await set_setting("triage_strategy", "llm")
    # Empty DB with no keys/models configured -> should gracefully fall back to rule-based
    difficulty = await grade_prompt_difficulty([
        {"role": "user", "content": "Please optimize:\n```python\nx = 1\n```"}
    ])
    assert difficulty >= 4
