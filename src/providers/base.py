from abc import ABC, abstractmethod
from typing import Tuple, Dict, Any


class BaseProvider(ABC):
    """
    Abstract interface specification for provider adapters.
    Isolates vendor-specific URL paths, headers, payload formats, and SSE stream parsing.
    """

    @abstractmethod
    def build_request(
        self, api_url: str, api_key: str, model_id: str, payload: Dict[str, Any]
    ) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
        """
        Constructs target URL, HTTP headers, and JSON body for the outbound request.
        Returns: (target_url, headers, outbound_json_payload)
        """
        pass

    @abstractmethod
    def parse_response(self, response_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalizes provider response JSON into standard OpenAI ChatCompletion format.
        """
        pass

    @abstractmethod
    def format_stream_chunk(
        self, raw_chunk: bytes, stream_state: Any = None
    ) -> Tuple[bytes, Dict[str, Any]]:
        """
        Normalizes raw streaming SSE chunk bytes into OpenAI data: {...} SSE format.
        Returns: (formatted_chunk_bytes, usage_info_dict)
        """
        pass

    def extract_quota_limits(self, platform: str, status_code: int, headers: Any = None, body_bytes: bytes = b"") -> Dict[str, Any]:
        """
        Optional hook for provider adapters to extract model quota ceilings (RPM, RPD, TPM, TPD)
        and quota exhaustion flags from HTTP headers or response body bytes.
        """
        return {"rpm_limit": None, "rpd_limit": None, "tpm_limit": None, "tpd_limit": None, "is_daily_exhausted": False}
