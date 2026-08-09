from src.config import logger
from src.providers.base import BaseProvider
from src.providers.openai_compat import OpenAIAdapter
from src.providers.google import GoogleAdapter

_openai_adapter = OpenAIAdapter()
_google_adapter = GoogleAdapter()


def get_provider(platform: str) -> BaseProvider:
    """
    Provider factory registry mapping platform identifier code to BaseProvider instance.
    """
    platform_lower = (platform or "").lower()
    if platform_lower == "google":
        logger.debug(f"[PROVIDER REGISTRY] Resolved GoogleAdapter for platform='{platform}'")
        return _google_adapter
    else:
        logger.debug(f"[PROVIDER REGISTRY] Resolved OpenAIAdapter for platform='{platform}'")
        return _openai_adapter
