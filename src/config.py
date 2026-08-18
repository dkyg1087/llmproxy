import os
import logging
import httpx
from dotenv import load_dotenv

load_dotenv()

# Setup central gateway logging format
LOG_LEVEL_STR = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_LEVEL = getattr(logging, LOG_LEVEL_STR, logging.INFO)

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)

logger = logging.getLogger("llm_gateway")
logger.setLevel(LOG_LEVEL)

# Cryptographic Master Secret Key for AES-256-GCM vault
GATEWAY_SECRET_KEY: str = os.getenv("GATEWAY_SECRET_KEY", "default_gateway_secret_key_32bytes_change_me!")

DB_PATH: str = os.getenv("DB_PATH", "router.db")
PORT: int = int(os.getenv("PORT", "8085"))

# BwK Router Bias Penalty Weights
GAP_BIAS: float = float(os.getenv("GAP_BIAS", "2.0"))
DURATION_BIAS: float = float(os.getenv("DURATION_BIAS", "0.2"))
USAGE_BIAS: float = float(os.getenv("USAGE_BIAS", "1.0"))

# Outbound Execution & Retry Limits
MAX_FAILOVER_RETRIES: int = int(os.getenv("MAX_FAILOVER_RETRIES", "3"))

# HTTP Client Timeout Settings (10s connect, 60s read)
CLIENT_TIMEOUT: httpx.Timeout = httpx.Timeout(10.0, read=60.0)

# Triage LLM Classifier Timeout (Default 10s)
TRIAGE_TIMEOUT_SECONDS: float = float(os.getenv("TRIAGE_TIMEOUT_SECONDS", "10.0"))
TRIAGE_CLIENT_TIMEOUT: httpx.Timeout = httpx.Timeout(5.0, read=TRIAGE_TIMEOUT_SECONDS)

# Debug Stage & Tracing
DEBUG_PAYLOADS: bool = os.getenv("DEBUG_PAYLOADS", "false").lower() in ("true", "1", "yes")
