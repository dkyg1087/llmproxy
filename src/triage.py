import re
import json
import time
import httpx
from typing import List, Dict, Any, Union, Optional
from src.config import logger, TRIAGE_CLIENT_TIMEOUT, TRIAGE_TIMEOUT_SECONDS
from src.db import get_setting, get_decrypted_key, log_request_usage
from src.router import select_model_and_platform
from src.providers.registry import get_provider

SYSTEM_PROMPT = """
You are a prompt difficulty classifier for an AI gateway.

Based on the provided prompt, rate its complexity from 1 to 5:
1: Casual chat, simple QA, definitions.
2: Basic translation, simple text editing, short rephrasing
3: Simple multi-step instructions, simple coding
4: Text summarization, moderate coding tasks, reasoning
5: Complex debugging, system design, refactoring

Output JSON only in this exact format:
{"difficulty": <1-5>, "reason": "<brief explanation>"}
"""


def grade_difficulty_by_rule(messages: Union[List[Dict[str, Any]], List[Any]]) -> int:
    """
    Structural complexity classifier based on prompt volume, syntax density,
    multi-step instructions, and tool usage (zero keyword dictionaries).
    """
    if not messages:
        return 2

    latest_user_text = ""
    has_tools = False
    total_words = 0

    for m in messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        tool_calls = m.get("tool_calls") if isinstance(m, dict) else getattr(m, "tool_calls", None)

        if tool_calls or role == "tool":
            has_tools = True

        msg_text = content if isinstance(content, str) else ""
        total_words += len(msg_text.split())
        if role == "user":
            latest_user_text = msg_text

    target_text = (latest_user_text or "").strip()
    words = target_text.split()
    prompt_len = len(words)

    # 1. Base Score by Prompt Volume
    if prompt_len <= 4:
        score = 1                     # Trivial ("hi", "yes", "ping")
    elif prompt_len <= 25:
        score = 2                     # Short query
    elif prompt_len <= 80:
        score = 3                     # Standard question / prompt
    elif prompt_len <= 200:
        score = 4                     # Detailed task / explanation
    else:
        score = 5                     # Extensive specification / long text

    # 2. Structural & Syntax Boosters
    lines = target_text.splitlines()

    # Code fences immediately indicate technical tasks (floor of 4)
    if "```" in target_text:
        score = max(score, 4)

    # Multi-step instructions (e.g. "1. ...", "2. ...", or multiple bullet points)
    numbered_steps = sum(1 for line in lines if line.strip()[:2] in ("1.", "2.", "3.", "4.", "5."))
    if numbered_steps >= 2:
        score = min(5, score + 1)

    # High code/data syntax density (curly braces, arrows, brackets)
    syntax_chars = sum(target_text.count(c) for c in ("{", "}", "->", "=>", "==", "!=", "::", "</"))
    if syntax_chars >= 4:
        score = min(5, score + 1)

    # 3. Tool Floor (Base tool use = 3)
    if has_tools:
        score = max(score, 3)

    # 4. Context Depth Booster (Deep conversation > 800 total words)
    if total_words > 800 and score < 5:
        score += 1

    final_score = max(1, min(5, score))
    logger.info(
        f"[TRIAGE RULE] Graded difficulty: {final_score} "
        f"(words={prompt_len}, total_words={total_words}, steps={numbered_steps}, syntax={syntax_chars}, tools={has_tools})"
    )
    return final_score


def sanitize_messages_for_triage(messages: List[Any], max_recent_turns: int = 4) -> List[Dict[str, str]]:
    """
    Sanitizes and windows conversation history for the triage LLM:
    - Extracts clean text from multipart blocks
    - Truncates oversized tool outputs to 250 chars
    - Windows to system message + last N turns
    - Merges consecutive same-role messages to satisfy strict API schemas
    """
    if not messages:
        return []

    # 1. Window messages: preserve system instructions + last N turns
    system_msgs = [m for m in messages if (m.get("role") if isinstance(m, dict) else getattr(m, "role", None)) == "system"]
    non_system = [m for m in messages if (m.get("role") if isinstance(m, dict) else getattr(m, "role", None)) != "system"]
    windowed = system_msgs + non_system[-max_recent_turns:]

    sanitized: List[Dict[str, str]] = []

    for m in windowed:
        if isinstance(m, dict):
            role = m.get("role", "user")
            content = m.get("content") or ""
            tool_calls = m.get("tool_calls")
        else:
            role = getattr(m, "role", "user")
            content = getattr(m, "content", "") or ""
            tool_calls = getattr(m, "tool_calls", None)

        # Extract text from multipart list or plain string
        text_content = ""
        if isinstance(content, str):
            text_content = content
        elif isinstance(content, list):
            text_content = " ".join(
                part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"
            )

        if role == "tool":
            tool_id = m.get("tool_call_id", "tool") if isinstance(m, dict) else getattr(m, "tool_call_id", "tool")
            # Truncate large tool output to 250 chars for triage
            preview = text_content[:250] + ("... [truncated]" if len(text_content) > 250 else "")
            sanitized.append({"role": "user", "content": f"[Tool result for {tool_id}]: {preview}"})

        elif tool_calls and isinstance(tool_calls, list):
            tool_names = []
            for call in tool_calls:
                fn_name = (
                    call.get("function", {}).get("name", "tool")
                    if isinstance(call, dict)
                    else getattr(getattr(call, "function", None), "name", "tool")
                )
                tool_names.append(fn_name)

            tool_summary = f"[Assistant invoked tools: {', '.join(tool_names)}]"
            full_content = f"{text_content}\n{tool_summary}".strip()
            sanitized.append({"role": "assistant", "content": full_content})
        else:
            sanitized.append({"role": role, "content": text_content.strip()})

    # 2. Merge consecutive same-role messages (e.g. user + tool result -> single user message)
    merged: List[Dict[str, str]] = []
    for msg in sanitized:
        if not msg["content"]:
            continue
        if merged and merged[-1]["role"] == msg["role"]:
            merged[-1]["content"] += "\n" + msg["content"]
        else:
            merged.append(msg)

    return merged


def _parse_triage_response(raw_text: str) -> Optional[int]:
    """Extracts integer difficulty (1-5) from model output using JSON or regex."""
    if not raw_text:
        return None
    try:
        data = json.loads(raw_text.strip())
        if "difficulty" in data:
            return max(1, min(5, int(data["difficulty"])))
    except Exception:
        pass

    match = re.search(r'"difficulty"\s*:\s*([1-5])', raw_text)
    if match:
        return int(match.group(1))
    return None


async def grade_prompt_difficulty(messages: Union[List[Dict[str, Any]], List[Any]]) -> int:
    """
    Primary difficulty classifier:
    - If strategy == 'heuristic', uses structural rule grading.
    - Otherwise, routes to the fastest healthy lightweight model (Score <= 3) via BwK.
    - Falls back to rule grading on timeout, rate limits, or provider errors.
    """
    strategy = await get_setting("triage_strategy", default="llm")
    if strategy == "heuristic":
        logger.info("[TRIAGE STRATEGY] Using rule-based classification.")
        return grade_difficulty_by_rule(messages)

    sanitized = sanitize_messages_for_triage(messages)
    est_prompt_tokens = sum(len(m.get("content", "")) for m in sanitized) // 4 + 60

    # 1. Select fast triage route
    triage_model = await get_setting("triage_model", default="auto")
    route = await select_model_and_platform(
        requested_model=triage_model,
        difficulty=1,
        max_base_score=3,
        gap_bias=0.0,
        duration_bias=2.5,
        estimated_prompt_tokens=est_prompt_tokens
    )

    # Dynamic fallback: if configured model is quarantined, pick any fast healthy model
    if not route and triage_model != "auto":
        logger.warning(f"[TRIAGE] Configured model '{triage_model}' unavailable, attempting auto-fallback...")
        route = await select_model_and_platform(
            requested_model="auto",
            difficulty=1,
            max_base_score=3,
            gap_bias=0.0,
            duration_bias=2.5,
            estimated_prompt_tokens=est_prompt_tokens
        )

    if not route:
        logger.warning("[TRIAGE] No fast models available for triage, falling back to rule-based.")
        return grade_difficulty_by_rule(messages)

    platform = route["platform"]
    model_id = route["model_id"]

    creds = await get_decrypted_key(platform)
    if not creds or not creds.get("api_url"):
        logger.warning(f"[TRIAGE] Credentials missing for '{platform}', falling back to rule-based.")
        return grade_difficulty_by_rule(messages)

    adapter = get_provider(platform)
    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            *sanitized
        ],
        "temperature": 0.0,
        "max_tokens": 100,
        "response_format": {"type": "json_object"}
    }

    target_url, headers, outbound_body = adapter.build_request(
        creds["api_url"], creds["api_key"], model_id, payload
    )

    start_time = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=TRIAGE_CLIENT_TIMEOUT) as client:
            logger.debug(f"[TRIAGE LLM] Sending classification request to {platform}/{model_id} (timeout={TRIAGE_TIMEOUT_SECONDS}s)...")
            response = await client.post(target_url, headers=headers, json=outbound_body)
            duration_ms = int((time.perf_counter() - start_time) * 1000)

            if response.status_code == 200:
                norm = adapter.parse_response(response.json())
                content = norm.get("choices", [{}])[0].get("message", {}).get("content", "")
                diff = _parse_triage_response(content)

                usage = norm.get("usage", {})
                tokens_in = usage.get("prompt_tokens", est_prompt_tokens)
                tokens_out = usage.get("completion_tokens", len(content) // 4)
                await log_request_usage(platform, model_id, tokens_in, tokens_out, duration_ms, request_success=True)

                if diff is not None:
                    logger.info(f"[TRIAGE LLM] Graded Difficulty={diff} via {platform}/{model_id} in {duration_ms}ms")
                    return diff
                else:
                    logger.warning(f"[TRIAGE PARSE FAILED] Content: '{content[:100]}', falling back to rule-based.")
                    return grade_difficulty_by_rule(messages)
            else:
                logger.warning(f"[TRIAGE HTTP {response.status_code}] {platform}/{model_id} failed, falling back to rule-based.")
                await log_request_usage(platform, model_id, 0, 0, duration_ms, request_success=False, error_type="provider_error")
                return grade_difficulty_by_rule(messages)

    except httpx.TimeoutException:
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        logger.warning(f"[TRIAGE TIMEOUT] Classifier exceeded {TRIAGE_TIMEOUT_SECONDS}s timeout, falling back to rule-based.")
        await log_request_usage(platform, model_id, 0, 0, duration_ms, request_success=False, error_type="timeout")
        return grade_difficulty_by_rule(messages)
    except Exception as e:
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        logger.warning(f"[TRIAGE FAILED] Exception ({e}), falling back to rule-based.")
        await log_request_usage(platform, model_id, 0, 0, duration_ms, request_success=False, error_type="provider_error")
        return grade_difficulty_by_rule(messages)

