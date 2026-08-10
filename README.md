# LLMProxy

An OpenAI-compatible LLM router and failover proxy. It routes prompt requests across platforms (such as Google Gemini, Groq, and custom OpenAI-compatible providers) using Bandits with Knapsacks (BwK) optimization and automatic failover when rate limits occur.

Includes an Admin Control Center dashboard for model catalog configuration, API key management, rate limit tracking, and system statistics.

---

## Disclaimer & Acknowledgements

- **Disclaimer**: This is a personal practice project built for learning and experimentation. Users are responsible for reviewing and complying with the Terms of Service (TOS) and regulations of all third-party API providers they use with this gateway.
- **Acknowledgements**: Please check out [freeLLMapi](https://github.com/tashfeenahmed/freellmapi), as this project is heavily influenced by their work.

---

## Features

- **OpenAI API Compatibility**: Works with standard OpenAI SDKs and client tools (`POST /v1/chat/completions`).
- **Bandits with Knapsacks (BwK) Router**: Solves model selection based on prompt difficulty, model base scores (0–5 scale), latency, and real-time quota usage.
- **Quota & Token Capacity Checking**: Compares estimated prompt tokens against remaining token capacity before sending requests.
- **Emergency Safety Net (Score 0)**: Reserves score `0` models as fallbacks when higher-scoring models are on rate limit cooldown.
- **Admin Dashboard**: Web interface to manage API keys, configure model catalog scores/limits, and view analytics.

---

## Quickstart

### 1. Installation

Install the required dependencies:

```bash
pip install -r requirements.txt
```

### 2. Run Server

Start the gateway server:

```bash
uvicorn src.main:app --port 8085
```

- **OpenAI Proxy Endpoint**: `http://localhost:8085/v1/chat/completions`
- **Admin Dashboard**: `http://localhost:8085/`

On first startup, open the Admin Dashboard (`http://localhost:8085/`) to enter your API keys and enable your models.

---

## API Usage Example

Configure any OpenAI-compatible client to point to `http://localhost:8085/v1`:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8085/v1",
    api_key="gateway-key"
)

response = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "Hello!"}],
    stream=True
)

for chunk in response:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
```

---

## Adding a Custom Platform Adapter

To add support for a custom LLM provider:

1. **Create Adapter (`src/providers/custom.py`)**:
   Inherit `BaseProvider` and implement request building and response normalization:
   ```python
   from src.providers.base import BaseProvider

   class CustomAdapter(BaseProvider):
       def build_request(self, api_url, api_key, model_id, payload): ...
       def parse_response(self, response_data): ...
       def format_stream_chunk(self, raw_chunk): ...
       def extract_quota_limits(self, platform, status_code, headers, body_bytes): ... # Optional quota parser
   ```

2. **Register in Provider Factory (`src/providers/registry.py`)**:
   Import your class and add an `elif` branch in `get_provider()`:
   ```python
   from src.providers.custom import CustomAdapter

   _custom_adapter = CustomAdapter()

   def get_provider(platform: str) -> BaseProvider:
       platform_lower = (platform or "").lower()
       if platform_lower == "google":
           return _google_adapter
       elif platform_lower == "custom":
           return _custom_adapter
       else:
           return _openai_adapter
   ```

3. **Add API Key & Models in Admin Dashboard**:
   - Go to **API Key Vault** and register a key with platform `"custom"`.
   - Go to **Model Catalog** and register models choosing platform `"custom"`.
