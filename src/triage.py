import json
import httpx
import aiosqlite
from typing import List, Dict, Any, Union
from src.config import logger, TRIAGE_CLIENT_TIMEOUT, TRIAGE_TIMEOUT_SECONDS
from src.db import get_setting, get_api_keys, is_cooldown_active

SYSTEM_PROMPT = """
You are a prompt difficulty classifier for an AI gateway.

Based on the provided prompt, rate its complexity from 1 to 5:
1: Casual chat, simple QA, definitions.
2: Basic translation, simple text editing, short rephrasing
3: Simple multi-step instructions, simple coding
4: Text summarization, moderate coding tasks, reasoning
5: Complex debugging, system design, refactoring
"""


def grade_difficulty_by_rule(messages: Union[List[Dict[str, Any]], List[Any]]) -> int:
    """
    Heuristic fallback classifier based on prompt word count,
    code blocks, tool calls, and reasoning keywords.
    """
    text_chunk = []
    has_tool_calls = False

    for m in messages:
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        tool_calls = m.get("tool_calls") if isinstance(m, dict) else getattr(m, "tool_calls", None)
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)

        if tool_calls or role == "tool":
            has_tool_calls = True

        if isinstance(content, str):
            text_chunk.append(content)

    full_text = "\n".join(text_chunk)
    score = 1

    if has_tool_calls:
        score += 1

    words = len(full_text.split())

    if words > 200:
        score += 1
    if words > 400:
        score += 1

    if "```" in full_text:
        score += 1

    reasoning_keywords = ["summarize", "optimize", "refactor", "create"]

    if any(kw in full_text.lower() for kw in reasoning_keywords):
        score += 1

    final_score = max(1, min(5, score))
    logger.info(f"[TRIAGE RULE] Graded prompt difficulty: {final_score} (words={words}, tool_calls={has_tool_calls})")
    return final_score


def sanitize_messages_for_triage(messages: List[Any]) -> List[Dict[str, str]]:
    """
    Sanitizes conversation history into {"role": ..., "content": ...} text blocks
    so triage LLM calls never trigger tool schema or thought signature validation errors.
    """
    sanitized: List[Dict[str, str]] = []

    for m in messages:
        if isinstance(m, dict):
            role = m.get("role", "user")
            content = m.get("content") or ""
            tool_calls = m.get("tool_calls")
        else:
            role = getattr(m, "role", "user")
            content = getattr(m, "content", "") or ""
            tool_calls = getattr(m, "tool_calls", None)

        if role == "tool":
            tool_id = m.get("tool_call_id", "tool") if isinstance(m, dict) else getattr(m, "tool_call_id", "tool")
            sanitized.append({"role": "user", "content": f"[Tool result for {tool_id}]: {content}"})
        elif tool_calls and isinstance(tool_calls, list):
            tool_names = []
            for call in tool_calls:
                if isinstance(call, dict):
                    fn_name = call.get("function", {}).get("name", "unknown_tool")
                else:
                    fn = getattr(call, "function", None)
                    fn_name = getattr(fn, "name", "unknown_tool") if fn else "unknown_tool"
                tool_names.append(fn_name)

            tool_summary = f"[Assistant invoked tools: {', '.join(tool_names)}]"
            full_content = f"{content}\n{tool_summary}".strip()
            sanitized.append({"role": "assistant", "content": full_content})
        else:
            sanitized.append({"role": role, "content": str(content)})

    return sanitized


async def grade_prompt_difficulty(conn: aiosqlite.Connection, messages: List[Any]) -> int:
    """
    Primary difficulty classifier using configured strategy.
    """
    strategy = await get_setting(conn, "triage_strategy", default="llm")
    if strategy == "heuristic":
        logger.info("[TRIAGE STRATEGY] Using rule-based classification.")
        return grade_difficulty_by_rule(messages)

    triage_platform = await get_setting(conn, "triage_platform", default="google")
    triage_model = await get_setting(conn, "triage_model", default="gemini-3.1-flash-lite")

    cur_model = await conn.execute(
        "SELECT enabled FROM models WHERE platform = ? AND model_id = ?",
        (triage_platform, triage_model)
    )
    m_row = await cur_model.fetchone()
    if not m_row or m_row[0] != 1:
        logger.warning(f"[TRIAGE WARNING] Triage model '{triage_platform}/{triage_model}' is disabled or missing in catalog, falling back to rule-based classification.")
        return grade_difficulty_by_rule(messages)

    creds = await get_api_keys(conn, triage_platform)

    if not creds or not creds.get("api_url"):
        logger.warning(f"[TRIAGE WARNING] Credentials missing for triage platform '{triage_platform}', falling back to rule-based classification.")
        return grade_difficulty_by_rule(messages)

    if await is_cooldown_active(conn, triage_platform, triage_model, creds["id"]):
        logger.warning(f"[TRIAGE WARNING] Triage model '{triage_platform}/{triage_model}' is in rate-limit cooldown, falling back to rule-based classification.")
        return grade_difficulty_by_rule(messages)

    target_url = f"{creds['api_url'].rstrip('/')}/chat/completions"
    target_headers = {
        "Authorization": f"Bearer {creds['api_key']}",
        "Content-Type": "application/json"
    }

    try:
        sanitized_messages = sanitize_messages_for_triage(messages)

        triage_payload = {
            "model": triage_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                *sanitized_messages
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "difficulty_classification",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "difficulty": {
                                "type": "integer",
                                "enum": [1, 2, 3, 4, 5],
                                "description": "Rating from 1 (simplest) to 5 (most complex)"
                            },
                            "reason": {
                                "type": "string",
                                "description": "Brief explanation for the reason."
                            }
                        },
                        "required": ["difficulty", "reason"],
                        "additionalProperties": False
                    }
                }
            }
        }

        async with httpx.AsyncClient(timeout=TRIAGE_CLIENT_TIMEOUT) as client:
            logger.debug(f"[TRIAGE LLM] Sending sanitized classification request (timeout={TRIAGE_TIMEOUT_SECONDS}s) to {triage_platform}/{triage_model}...")
            response = await client.post(target_url, headers=target_headers, json=triage_payload)

            if response.status_code == 200:
                res_data = response.json()
                raw_content = res_data["choices"][0]["message"]["content"]
                parsed = json.loads(raw_content)

                difficulty = int(parsed.get("difficulty", 2))
                reason = parsed.get("reason", "N/A")
                final_score = max(1, min(5, difficulty))
                logger.info(f"[TRIAGE LLM] Promt classified as Difficulty={final_score} (Reason: {reason})")
                return final_score
            else:
                logger.warning(f"[TRIAGE LLM FAILED] Returned status {response.status_code}, falling back to rule-based classification.")
                return grade_difficulty_by_rule(messages)

    except httpx.TimeoutException:
        logger.warning(f"[TRIAGE TIMEOUT] LLM classifier exceeded {TRIAGE_TIMEOUT_SECONDS}s timeout, falling back to rule-based classification.")
        return grade_difficulty_by_rule(messages)
    except Exception as e:
        logger.warning(f"[TRIAGE CALL FAILED] LLM failed ({str(e)}), falling back to rule-based classification.")
        return grade_difficulty_by_rule(messages)
