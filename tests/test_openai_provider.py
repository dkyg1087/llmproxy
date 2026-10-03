import json
import pytest
from src.providers.openai_compat import OpenAIAdapter


def test_openai_build_request_strips_google_artifacts():
    adapter = OpenAIAdapter()
    payload = {
        "model": "auto",
        "stream": True,
        "messages": [
            {"role": "user", "content": "What is the time?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_123__thought__AQID",
                        "type": "function",
                        "function": {"name": "get_time", "arguments": "{}"},
                        "thought_signature": "AQIDBAUGBw==",
                        "extra_content": {"thought_signature": "AQIDBAUGBw=="}
                    }
                ]
            },
            {
                "role": "tool",
                "tool_call_id": "call_123__thought__AQID",
                "content": '{"time": "12:00"}'
            }
        ]
    }

    url, headers, body = adapter.build_request("https://api.groq.com/openai/v1", "test_key", "llama-3.3-70b-versatile", payload)

    assert url == "https://api.groq.com/openai/v1/chat/completions"
    assert headers["Authorization"] == "Bearer test_key"
    assert body["model"] == "llama-3.3-70b-versatile"
    assert body["stream_options"] == {"include_usage": True}

    assistant_msg = body["messages"][1]
    tool_call = assistant_msg["tool_calls"][0]
    assert tool_call["id"] == "call_123"
    assert "thought_signature" not in tool_call
    assert "extra_content" not in tool_call

    tool_msg = body["messages"][2]
    assert tool_msg["tool_call_id"] == "call_123"


def test_openai_format_stream_chunk_extracts_usage():
    adapter = OpenAIAdapter()
    raw_event = (
        'data: {"id": "chatcmpl-123", "choices": [], '
        '"usage": {"prompt_tokens": 42, "completion_tokens": 17, "total_tokens": 59}}\n\n'
    ).encode("utf-8")

    chunk_bytes, usage_info = adapter.format_stream_chunk(raw_event)
    assert chunk_bytes == raw_event
    assert usage_info["prompt_tokens"] == 42
    assert usage_info["completion_tokens"] == 17


def test_openai_extract_quota_limits():
    adapter = OpenAIAdapter()
    headers = {
        "x-ratelimit-limit-requests": "1000",
        "x-ratelimit-limit-requests-day": "50000",
        "x-ratelimit-remaining-requests-day": "0",
        "x-ratelimit-limit-tokens": "200000",
        "x-ratelimit-limit-tokens-day": "1000000"
    }

    limits = adapter.extract_quota_limits("groq", 429, headers=headers)
    assert limits["rpm_limit"] == 1000
    assert limits["rpd_limit"] == 50000
    assert limits["is_daily_exhausted"] is True
    assert limits["tpm_limit"] == 200000
    assert limits["tpd_limit"] == 1000000
