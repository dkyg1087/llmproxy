from typing import Tuple, Dict, Any, List
from src.config import logger
from src.providers.base import BaseProvider


def strip_thought_signature_from_tool_call_id(tool_call_id: str) -> str:
    """Strips __thought__... suffix from tool_call_id for Gemini -> Non-Google handoffs."""
    if tool_call_id and "__thought__" in tool_call_id:
        clean_id = tool_call_id.split("__thought__")[0]
        logger.debug(f"[PROVIDER HANDOFF] Stripped thought signature from tool_call_id: '{tool_call_id}' -> '{clean_id}'")
        return clean_id
    return tool_call_id


class OpenAIAdapter(BaseProvider):
    """
    Standard OpenAI-compatible provider adapter.
    """

    def build_request(
        self, api_url: str, api_key: str, model_id: str, payload: Dict[str, Any]
    ) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
        target_url = f"{api_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

        outbound_body = dict(payload)
        outbound_body["model"] = model_id

        # Inject stream_options when streaming so providers return usage metrics in final SSE chunk
        if outbound_body.get("stream") is True:
            outbound_body["stream_options"] = {"include_usage": True}

        # Strip thought signatures from tool call IDs
        if "messages" in outbound_body and isinstance(outbound_body["messages"], list):
            cleaned_messages = []
            for msg in outbound_body["messages"]:
                if isinstance(msg, dict):
                    m_copy = dict(msg)
                    if m_copy.get("tool_call_id"):
                        m_copy["tool_call_id"] = strip_thought_signature_from_tool_call_id(m_copy["tool_call_id"])
                    if m_copy.get("tool_calls") and isinstance(m_copy["tool_calls"], list):
                        cleaned_calls = []
                        for call in m_copy["tool_calls"]:
                            c_copy = dict(call)
                            if c_copy.get("id"):
                                c_copy["id"] = strip_thought_signature_from_tool_call_id(c_copy["id"])
                            cleaned_calls.append(c_copy)
                        m_copy["tool_calls"] = cleaned_calls
                    cleaned_messages.append(m_copy)
                else:
                    cleaned_messages.append(msg)
            outbound_body["messages"] = cleaned_messages

        logger.debug(f"[PROVIDER OPENAI] Built request for model='{model_id}' at {target_url}")
        return target_url, headers, outbound_body

    def parse_response(self, response_data: Dict[str, Any]) -> Dict[str, Any]:
        return response_data

    def format_stream_chunk(self, raw_chunk: bytes) -> bytes:
        return raw_chunk

    def extract_quota_limits(self, platform: str, status_code: int, headers: Any = None, body_bytes: bytes = b"") -> Dict[str, Any]:
        """
        Extracts rate limit header values (RPM, RPD, TPM, TPD) from OpenAI-compatible HTTP headers.
        """
        limits: Dict[str, Any] = {"rpm_limit": None, "rpd_limit": None, "tpm_limit": None, "tpd_limit": None}
        if not headers:
            return limits

        if "x-ratelimit-limit-requests" in headers:
            try:
                limits["rpm_limit"] = int(headers["x-ratelimit-limit-requests"])
            except ValueError:
                pass

        if "x-ratelimit-limit-requests-day" in headers:
            try:
                limits["rpd_limit"] = int(headers["x-ratelimit-limit-requests-day"])
            except ValueError:
                pass

        if "x-ratelimit-limit-tokens" in headers:
            try:
                limits["tpm_limit"] = int(headers["x-ratelimit-limit-tokens"])
            except ValueError:
                pass

        if "x-ratelimit-limit-tokens-day" in headers:
            try:
                limits["tpd_limit"] = int(headers["x-ratelimit-limit-tokens-day"])
            except ValueError:
                pass

        return limits
