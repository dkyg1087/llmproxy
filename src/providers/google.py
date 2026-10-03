import time
import json
from typing import Tuple, Dict, Any, List, Optional
from src.config import logger
from src.providers.base import BaseProvider

THOUGHT_SIG_TTL_SECONDS = 1800  # 30 minutes
THOUGHT_SIG_MAX_SIZE = 5000
_thought_sig_cache: Dict[str, Dict[str, Any]] = {}
_tool_name_by_call_id: Dict[str, str] = {}


# ============================================================================
# Thought Signature & JSON Helper Utilities
# ============================================================================

def _canonical_args(args: Any) -> str:
    """Produces a canonical JSON string for tool arguments for consistent cache lookups."""
    if isinstance(args, str):
        try:
            return json.dumps(json.loads(args), sort_keys=True)
        except Exception:
            return args
    elif isinstance(args, dict):
        return json.dumps(args, sort_keys=True)
    return str(args or "")


def _safe_parse_args(args: Any) -> Dict[str, Any]:
    """Ensures tool call / response arguments are converted to a dictionary."""
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            val = json.loads(args)
            return val if isinstance(val, dict) else {"content": val}
        except Exception:
            return {"content": args}
    return {}


def remember_thought_sig(call_id: Optional[str], name: Optional[str], args: Any, sig: Optional[str]) -> None:
    """Caches a Google thoughtSignature indexed by call ID and (name, args)."""
    if not sig:
        return
    exp = time.time() + THOUGHT_SIG_TTL_SECONDS
    if len(_thought_sig_cache) >= THOUGHT_SIG_MAX_SIZE:
        oldest_key = next(iter(_thought_sig_cache))
        _thought_sig_cache.pop(oldest_key, None)

    if call_id:
        _thought_sig_cache[f"id:{call_id}"] = {"sig": sig, "exp": exp}
    if name:
        key = f"call:{name}:{_canonical_args(args)}"
        _thought_sig_cache[key] = {"sig": sig, "exp": exp}


def recall_thought_sig(call_id: Optional[str], name: Optional[str] = None, args: Any = None) -> Optional[str]:
    """Retrieves a cached thoughtSignature by call ID or (name, args) fallback."""
    now = time.time()
    for key in (f"id:{call_id}" if call_id else None, f"call:{name}:{_canonical_args(args)}" if name else None):
        if key and key in _thought_sig_cache:
            item = _thought_sig_cache[key]
            if item["exp"] > now:
                return item["sig"]
            _thought_sig_cache.pop(key, None)
    return None


# ============================================================================
# Outbound Request Transformation Helpers
# ============================================================================

def _convert_messages(messages: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """
    Translates OpenAI chat messages into Gemini contents and system instructions.
    Injects thoughtSignatures or 'skip_thought_signature_validator' on function calls.
    """
    contents: List[Dict[str, Any]] = []
    system_instruction_parts: List[Dict[str, str]] = []

    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "user")
        content_str = str(m.get("content") or "")

        if role == "system":
            if content_str:
                system_instruction_parts.append({"text": content_str})

        elif role == "user":
            if content_str:
                contents.append({"role": "user", "parts": [{"text": content_str}]})

        elif role == "assistant":
            parts: List[Dict[str, Any]] = []
            if content_str:
                parts.append({"text": content_str})

            tool_calls = m.get("tool_calls") or []
            if isinstance(tool_calls, list):
                for call in tool_calls:
                    if not isinstance(call, dict):
                        continue
                    call_id = call.get("id")
                    fn_obj = call.get("function", {}) if isinstance(call.get("function"), dict) else {}
                    fn_name = fn_obj.get("name", "unknown_tool")
                    fn_args = _safe_parse_args(fn_obj.get("arguments"))

                    if call_id:
                        if len(_tool_name_by_call_id) >= THOUGHT_SIG_MAX_SIZE:
                            oldest_id = next(iter(_tool_name_by_call_id))
                            _tool_name_by_call_id.pop(oldest_id, None)
                        _tool_name_by_call_id[call_id] = fn_name

                    sig = (
                        call.get("thought_signature")
                        or (call.get("extra_content", {}) if isinstance(call.get("extra_content"), dict) else {}).get("thought_signature")
                        or recall_thought_sig(call_id, fn_name, fn_args)
                        or "skip_thought_signature_validator"
                    )

                    parts.append({
                        "thoughtSignature": sig,
                        "functionCall": {
                            "id": call_id,
                            "name": fn_name,
                            "args": fn_args
                        }
                    })

            if parts:
                contents.append({"role": "model", "parts": parts})

        elif role == "tool":
            tool_call_id = m.get("tool_call_id")
            if tool_call_id:
                tool_name = m.get("name") or _tool_name_by_call_id.get(tool_call_id) or "tool"
                response_obj = _safe_parse_args(m.get("content"))
                func_part = {
                    "functionResponse": {
                        "id": tool_call_id,
                        "name": tool_name,
                        "response": response_obj
                    }
                }
                # Group consecutive tool responses under a single user turn if adjacent
                if contents and contents[-1].get("role") == "user" and any("functionResponse" in p for p in contents[-1].get("parts", [])):
                    contents[-1]["parts"].append(func_part)
                else:
                    contents.append({
                        "role": "user",
                        "parts": [func_part]
                    })

    return contents, system_instruction_parts


def _convert_tools(openai_tools: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
    """Translates OpenAI tools schema to Gemini functionDeclarations."""
    if not isinstance(openai_tools, list) or not openai_tools:
        return None

    function_declarations = []
    for tool in openai_tools:
        if isinstance(tool, dict) and tool.get("type") == "function":
            fn = tool.get("function", {})
            function_declarations.append({
                "name": fn.get("name"),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {"type": "object", "properties": {}})
            })

    if function_declarations:
        return [{"functionDeclarations": function_declarations}]
    return None


def _convert_generation_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Maps OpenAI generation parameters (temperature, max_tokens, response_format) to Gemini generationConfig."""
    gen_config: Dict[str, Any] = {}
    if "temperature" in payload:
        gen_config["temperature"] = payload["temperature"]
    if "top_p" in payload:
        gen_config["topP"] = payload["top_p"]
    if "max_tokens" in payload:
        try:
            gen_config["maxOutputTokens"] = int(payload["max_tokens"])
        except (ValueError, TypeError):
            pass
    if "response_format" in payload and isinstance(payload["response_format"], dict):
        fmt_type = payload["response_format"].get("type")
        if fmt_type in ("json_object", "json_schema"):
            gen_config["responseMimeType"] = "application/json"
    return gen_config


# ============================================================================
# Stream Reassembly and Parsing Helpers
# ============================================================================

def _reassemble_stream_lines(raw_chunk: bytes, stream_state: Optional[Dict[str, Any]]) -> List[str]:
    """
    Decodes raw chunk bytes and reassembles fragmented TCP lines.
    Maintains line buffer inside stream_state to avoid dropped SSE frames across chunk boundaries.
    """
    incoming_str = raw_chunk.decode("utf-8", errors="ignore")
    state = stream_state if stream_state is not None else {}
    buffer = state.get("buffer", "") + incoming_str

    if not buffer:
        return []

    if "\n" not in buffer:
        state["buffer"] = buffer
        return []

    lines = buffer.split("\n")
    if buffer.endswith("\n"):
        complete_lines = lines[:-1]
        state["buffer"] = ""
    else:
        complete_lines = lines[:-1]
        state["buffer"] = lines[-1]

    return complete_lines


def _parse_candidate_parts(parts: List[Dict[str, Any]], stream_state: Optional[Dict[str, Any]] = None) -> Tuple[List[str], List[Dict[str, Any]]]:
    """
    Extracts text and tool_calls from Gemini candidate parts.
    Caches thoughtSignatures and numbers tool calls according to stream_state.
    """
    text_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    fallback_idx = 0
    state = stream_state if stream_state is not None else {}

    for part in parts:
        if not isinstance(part, dict):
            continue

        if part.get("thoughtSignature"):
            state["last_thought_signature"] = part["thoughtSignature"]

        if part.get("text"):
            text_parts.append(part["text"])

        fn_call = part.get("functionCall")
        if isinstance(fn_call, dict) and fn_call.get("name"):
            call_id = fn_call.get("id") or f"call_gemini_{int(time.time())}_{fallback_idx}"
            fallback_idx += 1
            fn_name = fn_call["name"]
            fn_args_dict = fn_call.get("args") or {}
            fn_args_str = json.dumps(fn_args_dict)
            sig = part.get("thoughtSignature") or state.get("last_thought_signature")

            if sig:
                remember_thought_sig(call_id, fn_name, fn_args_str, sig)

            if stream_state is not None:
                stream_state["has_tool_calls"] = True
                tool_idx = stream_state.get("tool_call_index", 0)
                stream_state["tool_call_index"] = tool_idx + 1
            else:
                tool_idx = fallback_idx - 1

            tool_calls.append({
                "index": tool_idx,
                "id": call_id,
                "type": "function",
                "function": {
                    "name": fn_name,
                    "arguments": fn_args_str
                },
                "thought_signature": sig
            })

    return text_parts, tool_calls


# ============================================================================
# Google Adapter Class
# ============================================================================

class GoogleAdapter(BaseProvider):
    """
    Native Google Gemini REST API Provider Adapter.
    Translates between OpenAI completions schema and Gemini REST v1beta endpoints.
    Fully stateless across requests; tracks per-stream buffers in stream_state.
    """

    def build_request(
        self, api_url: str, api_key: str, model_id: str, payload: Dict[str, Any]
    ) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
        is_stream = payload.get("stream") is True
        endpoint = "streamGenerateContent?alt=sse" if is_stream else "generateContent"
        target_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:{endpoint}"

        headers = {
            "x-goog-api-key": api_key,
            "Content-Type": "application/json"
        }

        contents, system_instruction_parts = _convert_messages(payload.get("messages", []))
        outbound_body: Dict[str, Any] = {"contents": contents}

        if system_instruction_parts:
            outbound_body["systemInstruction"] = {"parts": system_instruction_parts}

        tools_decl = _convert_tools(payload.get("tools"))
        if tools_decl:
            outbound_body["tools"] = tools_decl

        gen_config = _convert_generation_config(payload)
        if gen_config:
            outbound_body["generationConfig"] = gen_config

        logger.debug(f"[PROVIDER GOOGLE NATIVE] Built native Gemini request for model='{model_id}' at {target_url}")
        return target_url, headers, outbound_body

    def parse_response(self, response_data: Dict[str, Any]) -> Dict[str, Any]:
        """Parses native Gemini REST response and normalizes to standard OpenAI ChatCompletion format."""
        candidate = (response_data.get("candidates") or [{}])[0]
        parts = (candidate.get("content", {}) if isinstance(candidate.get("content"), dict) else {}).get("parts") or []

        text_parts, tool_calls = _parse_candidate_parts(parts, stream_state=None)

        # In non-streaming OpenAI chat completions, choices[0].message.tool_calls omit the stream "index"
        clean_tool_calls = [
            {
                "id": tc["id"],
                "type": "function",
                "function": tc["function"],
                "thought_signature": tc.get("thought_signature")
            }
            for tc in tool_calls
        ] if tool_calls else None

        usage_meta = response_data.get("usageMetadata", {})
        usage = {
            "prompt_tokens": usage_meta.get("promptTokenCount", 0),
            "completion_tokens": usage_meta.get("candidatesTokenCount", 0),
            "total_tokens": usage_meta.get("totalTokenCount", 0)
        }

        return {
            "id": f"chatcmpl-gemini-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "gemini",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "".join(text_parts) if text_parts else None,
                        "tool_calls": clean_tool_calls
                    },
                    "finish_reason": "tool_calls" if tool_calls else "stop"
                }
            ],
            "usage": usage
        }

    def format_stream_chunk(
        self, raw_chunk: bytes, stream_state: Optional[Dict[str, Any]] = None
    ) -> Tuple[bytes, Dict[str, Any]]:
        """
        Parses native Gemini SSE chunks and yields standard OpenAI SSE lines.
        Handles TCP packet fragmentation and maintains 0-based tool indexing.
        """
        complete_lines = _reassemble_stream_lines(raw_chunk, stream_state)
        if not complete_lines:
            return b"", {}

        output_sse: List[str] = []
        usage_info: Dict[str, Any] = {}
        total_delta_chars = 0

        chunk_id = (stream_state.get("stream_id") if stream_state else None) or f"chatcmpl-gemini-stream-{int(time.time())}"
        created_ts = (stream_state.get("created") if stream_state else None) or int(time.time())
        model_name = (stream_state.get("model_id") if stream_state else None) or "gemini"

        for line in complete_lines:
            trimmed = line.strip()
            if not trimmed or not trimmed.startswith("data:"):
                continue

            json_text = trimmed[5:].strip()
            if not json_text or json_text == "[DONE]":
                output_sse.append("data: [DONE]\n\n")
                continue

            try:
                data = json.loads(json_text)

                usage_meta = data.get("usageMetadata")
                if isinstance(usage_meta, dict):
                    if "promptTokenCount" in usage_meta:
                        usage_info["prompt_tokens"] = usage_meta["promptTokenCount"]
                    if "candidatesTokenCount" in usage_meta:
                        usage_info["completion_tokens"] = usage_meta["candidatesTokenCount"]

                candidate = (data.get("candidates") or [{}])[0]
                parts = (candidate.get("content", {}) if isinstance(candidate.get("content"), dict) else {}).get("parts") or []

                text_parts, tool_calls = _parse_candidate_parts(parts, stream_state=stream_state)

                delta: Dict[str, Any] = {}
                if stream_state is not None and not stream_state.get("role_sent", False):
                    delta["role"] = "assistant"
                    stream_state["role_sent"] = True

                if text_parts:
                    content_str = "".join(text_parts)
                    delta["content"] = content_str
                    total_delta_chars += len(content_str)
                elif tool_calls and "role" in delta:
                    delta["content"] = None

                if tool_calls:
                    delta["tool_calls"] = tool_calls
                    total_delta_chars += sum(len(tc["function"]["name"]) + len(tc["function"]["arguments"]) for tc in tool_calls)

                gemini_finish = candidate.get("finishReason")
                if gemini_finish:
                    if (stream_state and stream_state.get("has_tool_calls")) or tool_calls:
                        finish_reason = "tool_calls"
                    else:
                        finish_reason = gemini_finish.lower()
                else:
                    finish_reason = None

                if not delta and finish_reason is None:
                    continue

                openai_chunk = {
                    "id": chunk_id,
                    "object": "chat.completion.chunk",
                    "created": created_ts,
                    "model": model_name,
                    "choices": [
                        {
                            "index": 0,
                            "delta": delta,
                            "finish_reason": finish_reason
                        }
                    ]
                }
                output_sse.append(f"data: {json.dumps(openai_chunk)}\n\n")

                if finish_reason is not None:
                    output_sse.append("data: [DONE]\n\n")
                    if stream_state is not None:
                        stream_state["done_sent"] = True

            except Exception as e:
                logger.warning(f"[GOOGLE STREAM] Failed to parse SSE JSON frame: {e}")
                continue

        usage_info["delta_chars"] = total_delta_chars
        return "".join(output_sse).encode("utf-8"), usage_info

    def extract_quota_limits(
        self, platform: str, status_code: int, headers: Any = None, body_bytes: bytes = b""
    ) -> Dict[str, Any]:
        """Extracts RPM, RPD, TPM, and TPD limits from Google RPC QuotaFailure body on HTTP 429."""
        limits: Dict[str, Any] = {
            "rpm_limit": None,
            "rpd_limit": None,
            "tpm_limit": None,
            "tpd_limit": None,
            "is_daily_exhausted": False
        }
        if status_code != 429 or not body_bytes:
            return limits

        try:
            data = json.loads(body_bytes.decode("utf-8"))
            error_obj = data.get("error", {})
            error_message = error_obj.get("message", "")
            details = error_obj.get("details", [])

            for detail in details:
                if "QuotaFailure" in detail.get("@type", ""):
                    for v in detail.get("violations", []):
                        q_id = v.get("quotaId", "")
                        q_desc = v.get("description", "")
                        q_val = v.get("quotaValue")
                        if q_val:
                            val = int(q_val)
                            if "RequestsPerMinute" in q_id:
                                limits["rpm_limit"] = val
                            elif "RequestsPerDay" in q_id:
                                limits["rpd_limit"] = val
                            elif "TokensPerMinute" in q_id:
                                limits["tpm_limit"] = val
                            elif "TokensPerDay" in q_id:
                                limits["tpd_limit"] = val
                            logger.info(f"[GOOGLE PARSER] Parsed dynamic limit from 429 QuotaFailure: {q_id} = {val}")

                        if "RequestsPerDay" in q_id or "PerDay" in q_id or "PerDay" in q_desc:
                            limits["is_daily_exhausted"] = True
                            logger.warning(f"[GOOGLE PARSER] Detected daily quota exhaustion: {q_id}")

            if "PerDay" in error_message or "per day" in error_message.lower():
                limits["is_daily_exhausted"] = True
        except Exception as e:
            logger.warning(f"[GOOGLE PARSER] Failed to parse 429 quota failure body: {e}")

        return limits
