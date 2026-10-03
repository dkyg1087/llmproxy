import json
import pytest
from src.providers.google import (
    GoogleAdapter,
    remember_thought_sig,
    recall_thought_sig,
    _canonical_args,
    _safe_parse_args,
)


def test_thought_sig_cache_and_canonicalization():
    remember_thought_sig("call_1", "get_weather", '{"city": "Paris"}', "sig_paris")
    
    # Direct recall by call_id
    assert recall_thought_sig("call_1") == "sig_paris"
    
    # Recall by (name, args) with differing whitespace
    assert recall_thought_sig(None, "get_weather", '{"city":   "Paris"}') == "sig_paris"
    assert recall_thought_sig(None, "get_weather", {"city": "Paris"}) == "sig_paris"


def test_google_build_request_translation():
    adapter = GoogleAdapter()
    payload = {
        "model": "gemini-2.5-flash",
        "stream": True,
        "temperature": 0.7,
        "max_tokens": 1024,
        "messages": [
            {"role": "system", "content": "You are a helpful coding assistant."},
            {"role": "user", "content": "Run tests"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_abc",
                        "type": "function",
                        "function": {"name": "run_command", "arguments": '{"cmd": "pytest"}'}
                    }
                ]
            },
            {
                "role": "tool",
                "tool_call_id": "call_abc",
                "name": "run_command",
                "content": '{"output": "all passed"}'
            }
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "run_command",
                    "description": "Executes shell commands",
                    "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}
                }
            }
        ]
    }

    url, headers, body = adapter.build_request("https://dummy", "fake_key", "gemini-2.5-flash", payload)

    assert "streamGenerateContent?alt=sse" in url
    assert headers["x-goog-api-key"] == "fake_key"
    assert body["systemInstruction"]["parts"][0]["text"] == "You are a helpful coding assistant."
    
    # Check function call and fallback signature injection
    contents = body["contents"]
    model_turn = next(c for c in contents if c["role"] == "model")
    fn_call_part = model_turn["parts"][0]
    assert fn_call_part["functionCall"]["name"] == "run_command"
    assert fn_call_part["thoughtSignature"] == "skip_thought_signature_validator"

    # Check function response
    user_turn = contents[-1]
    assert user_turn["role"] == "user"
    assert user_turn["parts"][0]["functionResponse"]["id"] == "call_abc"

    # Check tools conversion
    assert body["tools"][0]["functionDeclarations"][0]["name"] == "run_command"
    assert body["generationConfig"]["temperature"] == 0.7
    assert body["generationConfig"]["maxOutputTokens"] == 1024


def test_google_parse_response_non_streaming():
    adapter = GoogleAdapter()
    gemini_resp = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"thoughtSignature": "sig_response_xyz"},
                        {"text": "Executing command now..."},
                        {
                            "functionCall": {
                                "id": "call_res_1",
                                "name": "run_command",
                                "args": {"cmd": "ls -la"}
                            }
                        }
                    ],
                    "role": "model"
                },
                "finishReason": "STOP"
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 15,
            "candidatesTokenCount": 12,
            "totalTokenCount": 27
        }
    }

    parsed = adapter.parse_response(gemini_resp)
    assert parsed["choices"][0]["message"]["role"] == "assistant"
    assert parsed["choices"][0]["message"]["content"] == "Executing command now..."
    assert len(parsed["choices"][0]["message"]["tool_calls"]) == 1
    assert parsed["choices"][0]["finish_reason"] == "tool_calls"
    assert parsed["usage"]["total_tokens"] == 27

    # Verify signature was remembered in cache
    assert recall_thought_sig("call_res_1") == "sig_response_xyz"
