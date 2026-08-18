import time
import json
from typing import Tuple, Dict, Any, List, Optional
from src.config import logger
from src.providers.base import BaseProvider

THOUGHT_SIG_TTL_SECONDS = 1800  # 30 minutes
THOUGHT_SIG_MAX_SIZE = 5000
_thought_sig_cache: Dict[str, Dict[str, Any]] = {}
_tool_name_by_call_id: Dict[str, str] = {}


def _canonical_args(args: Any) -> str:
    if isinstance(args, str):
        try:
            return json.dumps(json.loads(args), sort_keys=True)
        except Exception:
            return args
    elif isinstance(args, dict):
        return json.dumps(args, sort_keys=True)
    return str(args or "")


def remember_thought_sig(call_id: Optional[str], name: Optional[str], args: Any, sig: Optional[str]) -> None:
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
    now = time.time()
    for key in (f"id:{call_id}" if call_id else None, f"call:{name}:{_canonical_args(args)}" if name else None):
        if key and key in _thought_sig_cache:
            item = _thought_sig_cache[key]
            if item["exp"] > now:
                return item["sig"]
            else:
                _thought_sig_cache.pop(key, None)
    return None


def _safe_parse_args(args: Any) -> Dict[str, Any]:
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            val = json.loads(args)
            return val if isinstance(val, dict) else {"content": val}
        except Exception:
            return {"content": args}
    return {}


class GoogleAdapter(BaseProvider):
    """
    Native Google Gemini REST API Provider Adapter.
    Tracks thoughtSignatures across streaming frames and supplies skip_thought_signature_validator for un-cached turns.
    """

    def __init__(self):
        self._last_stream_sig: Optional[str] = None

    def build_request(
        self, api_url: str, api_key: str, model_id: str, payload: Dict[str, Any]
    ) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
        self._last_stream_sig = None

        is_stream = payload.get("stream") is True
        if is_stream:
            target_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:streamGenerateContent?alt=sse"
        else:
            target_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent"

        headers = {
            "x-goog-api-key": api_key,
            "Content-Type": "application/json"
        }

        contents: List[Dict[str, Any]] = []
        system_instruction_parts: List[Dict[str, str]] = []

        messages = payload.get("messages", [])
        for m in messages:
            if not isinstance(m, dict):
                continue
            role = m.get("role", "user")
            content_str = str(m.get("content") or "")

            if role == "system":
                if content_str:
                    system_instruction_parts.append({"text": content_str})

            elif role == "user":
                parts: List[Dict[str, Any]] = []
                if content_str:
                    parts.append({"text": content_str})
                if parts:
                    contents.append({"role": "user", "parts": parts})

            elif role == "assistant":
                parts = []
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
                            _tool_name_by_call_id[call_id] = fn_name

                        sig = (
                            call.get("thought_signature")
                            or (call.get("extra_content", {}) if isinstance(call.get("extra_content"), dict) else {}).get("thought_signature")
                            or recall_thought_sig(call_id, fn_name, fn_args)
                            or "skip_thought_signature_validator"
                        )

                        part_entry: Dict[str, Any] = {
                            "thoughtSignature": sig,
                            "functionCall": {
                                "id": call_id,
                                "name": fn_name,
                                "args": fn_args
                            }
                        }

                        parts.append(part_entry)

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
                    if contents and contents[-1].get("role") == "user" and any("functionResponse" in p for p in contents[-1].get("parts", [])):
                        contents[-1]["parts"].append(func_part)
                    else:
                        contents.append({
                            "role": "user",
                            "parts": [func_part]
                        })

        outbound_body: Dict[str, Any] = {"contents": contents}

        if system_instruction_parts:
            outbound_body["systemInstruction"] = {"parts": system_instruction_parts}

        openai_tools = payload.get("tools")
        if isinstance(openai_tools, list) and openai_tools:
            function_declarations = []
            for tool in openai_tools:
                if isinstance(tool, dict) and tool.get("type") == "function":
                    fn = tool.get("function", {})
                    fn_decl = {
                        "name": fn.get("name"),
                        "description": fn.get("description", ""),
                        "parameters": fn.get("parameters", {"type": "object", "properties": {}})
                    }
                    function_declarations.append(fn_decl)
            if function_declarations:
                outbound_body["tools"] = [{"functionDeclarations": function_declarations}]
                
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
        if gen_config:
            outbound_body["generationConfig"] = gen_config

        logger.debug(f"[PROVIDER GOOGLE NATIVE] Built native Gemini request for model='{model_id}' at {target_url}")
        return target_url, headers, outbound_body

    def parse_response(self, response_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parses native Gemini REST response and converts it to standard OpenAI chat completion JSON.
        """
        candidate = (response_data.get("candidates") or [{}])[0]
        parts = (candidate.get("content", {}) if isinstance(candidate.get("content"), dict) else {}).get("parts") or []

        text_parts = []
        tool_calls = []
        fallback_idx = 0

        last_sig: Optional[str] = None
        for part in parts:
            if not isinstance(part, dict):
                continue

            if part.get("thoughtSignature"):
                last_sig = part["thoughtSignature"]

            if part.get("text"):
                text_parts.append(part["text"])

            fn_call = part.get("functionCall")
            if isinstance(fn_call, dict) and fn_call.get("name"):
                call_id = fn_call.get("id") or f"call_gemini_{int(time.time())}_{fallback_idx}"
                fallback_idx += 1
                fn_name = fn_call["name"]
                fn_args_dict = fn_call.get("args") or {}
                fn_args_str = json.dumps(fn_args_dict)
                sig = part.get("thoughtSignature") or last_sig

                if sig:
                    remember_thought_sig(call_id, fn_name, fn_args_str, sig)

                tool_calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": fn_name,
                        "arguments": fn_args_str
                    },
                    "thought_signature": sig
                })

        content_text = "".join(text_parts) if text_parts else None

        usage_meta = response_data.get("usageMetadata", {})
        usage = {
            "prompt_tokens": usage_meta.get("promptTokenCount", 0),
            "completion_tokens": usage_meta.get("candidatesTokenCount", 0),
            "total_tokens": usage_meta.get("totalTokenCount", 0)
        }

        finish_reason = "tool_calls" if tool_calls else "stop"

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
                        "content": content_text,
                        "tool_calls": tool_calls if tool_calls else None
                    },
                    "finish_reason": finish_reason
                }
            ],
            "usage": usage
        }

    def format_stream_chunk(self, raw_chunk: bytes) -> Tuple[bytes, Dict[str, Any]]:
        """
        Parses native Gemini SSE lines ('data: {...}') and converts them into standard OpenAI SSE chunks.
        Tracks thoughtSignature across SSE frames and extracts usageMetadata.
        Returns: (formatted_sse_bytes, usage_info_dict)
        """
        chunk_str = raw_chunk.decode("utf-8", errors="ignore").strip()
        if not chunk_str:
            return b"", {}

        lines = chunk_str.split("\n")
        output_sse = []
        usage_info: Dict[str, Any] = {}
        total_delta_chars = 0

        for line in lines:
            trimmed = line.strip()
            if not trimmed.startswith("data:"):
                continue
            json_text = trimmed[5:].strip()
            if not json_text or json_text == "[DONE]":
                output_sse.append("data: [DONE]\n\n")
                continue

            try:
                data = json.loads(json_text)
                
                # Extract official usage metadata if available
                usage_meta = data.get("usageMetadata")
                if isinstance(usage_meta, dict):
                    if "promptTokenCount" in usage_meta:
                        usage_info["prompt_tokens"] = usage_meta["promptTokenCount"]
                    if "candidatesTokenCount" in usage_meta:
                        usage_info["completion_tokens"] = usage_meta["candidatesTokenCount"]

                candidate = (data.get("candidates") or [{}])[0]
                parts = (candidate.get("content", {}) if isinstance(candidate.get("content"), dict) else {}).get("parts") or []

                text_parts = []
                tool_calls = []
                fallback_idx = 0

                for part in parts:
                    if not isinstance(part, dict):
                        continue

                    if part.get("thoughtSignature"):
                        self._last_stream_sig = part["thoughtSignature"]

                    if part.get("text"):
                        text_parts.append(part["text"])

                    fn_call = part.get("functionCall")
                    if isinstance(fn_call, dict) and fn_call.get("name"):
                        call_id = fn_call.get("id") or f"call_gemini_{int(time.time())}_{fallback_idx}"
                        fallback_idx += 1
                        fn_name = fn_call["name"]
                        fn_args_dict = fn_call.get("args") or {}
                        fn_args_str = json.dumps(fn_args_dict)
                        sig = part.get("thoughtSignature") or self._last_stream_sig

                        if sig:
                            remember_thought_sig(call_id, fn_name, fn_args_str, sig)

                        tool_calls.append({
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": fn_name,
                                "arguments": fn_args_str
                            },
                            "thought_signature": sig
                        })

                delta: Dict[str, Any] = {}
                if text_parts:
                    content_str = "".join(text_parts)
                    delta["content"] = content_str
                    total_delta_chars += len(content_str)
                if tool_calls:
                    delta["tool_calls"] = tool_calls
                    total_delta_chars += sum(len(tc["function"]["name"]) + len(tc["function"]["arguments"]) for tc in tool_calls)

                finish_reason = "tool_calls" if tool_calls else (candidate.get("finishReason", "").lower() if candidate.get("finishReason") else None)

                openai_chunk = {
                    "id": f"chatcmpl-gemini-stream-{int(time.time())}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": "gemini",
                    "choices": [
                        {
                            "index": 0,
                            "delta": delta,
                            "finish_reason": finish_reason
                        }
                    ]
                }
                output_sse.append(f"data: {json.dumps(openai_chunk)}\n\n")

            except Exception:
                continue

        usage_info["delta_chars"] = total_delta_chars
        return "".join(output_sse).encode("utf-8"), usage_info

    def extract_quota_limits(self, platform: str, status_code: int, headers: Any = None, body_bytes: bytes = b"") -> Dict[str, Optional[int]]:
        """Extracts RPM, RPD, TPM, and TPD limits from Google RPC QuotaFailure body on 429."""
        limits: Dict[str, Optional[int]] = {"rpm_limit": None, "rpd_limit": None, "tpm_limit": None, "tpd_limit": None}
        if status_code != 429 or not body_bytes:
            return limits

        try:
            data = json.loads(body_bytes.decode("utf-8"))
            details = data.get("error", {}).get("details", [])
            for detail in details:
                if "QuotaFailure" in detail.get("@type", ""):
                    for v in detail.get("violations", []):
                        q_id = v.get("quotaId", "")
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
        except Exception as e:
            logger.warning(f"[GOOGLE PARSER] Failed to parse 429 quota failure body: {e}")

        return limits
