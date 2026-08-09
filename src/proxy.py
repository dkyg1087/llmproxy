import time
import json
import httpx
import aiosqlite
from typing import Optional, Dict, Any, AsyncGenerator
from fastapi.responses import StreamingResponse, JSONResponse

from src.config import logger, MAX_FAILOVER_RETRIES, CLIENT_TIMEOUT
from src.models import ChatCompletionRequest
from src.db import (
    get_db_connection,
    get_api_keys,
    log_request_usage,
    add_cooldown,
    update_model_limits,
    disable_key,
    mark_model_incapable,
)
from src.router import select_model_and_platform
from src.providers.registry import get_provider




def estimate_prompt_tokens(payload_dict: Dict[str, Any]) -> int:
    """Calculates an accurate initial estimation of prompt input tokens."""
    total_chars = 0
    for msg in payload_dict.get("messages", []):
        content = msg.get("content")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("text"):
                    total_chars += len(part["text"])
    tools = payload_dict.get("tools")
    if tools:
        total_chars += len(json.dumps(tools))
    return max(5, total_chars // 4)


async def stream_generator(
    response: httpx.Response,
    key_id: int,
    platform: str,
    model_id: str,
    start_time: float,
    adapter: Any,
    initial_input_tokens: int = 0
) -> AsyncGenerator[bytes, None]:
    """
    Async SSE stream generator that streams chunks to client, tracks TTFT,
    logs prompt/completion token usage on completion, and handles failures.
    """
    ttft_ms: Optional[int] = None
    input_tokens = initial_input_tokens
    output_tokens = 0
    stream_success = True

    try:
        async for raw_chunk in response.aiter_bytes():
            if ttft_ms is None:
                ttft_ms = int((time.perf_counter() - start_time) * 1000)
                logger.info(f"[PROXY STREAM] First token received (TTFT: {ttft_ms}ms) for {platform}/{model_id}")

            res = adapter.format_stream_chunk(raw_chunk)
            if isinstance(res, tuple):
                formatted_chunk, usage_info = res
            else:
                formatted_chunk, usage_info = res, {}

            if formatted_chunk:
                yield formatted_chunk

            if usage_info.get("prompt_tokens") is not None:
                input_tokens = usage_info["prompt_tokens"]
            if usage_info.get("completion_tokens") is not None:
                output_tokens = usage_info["completion_tokens"]
            elif usage_info.get("delta_chars"):
                output_tokens += max(1, usage_info["delta_chars"] // 4)

    except Exception as e:
        stream_success = False
        logger.error(f"[PROXY STREAM ERROR] Mid-stream connection dropped ({str(e)})")
        err_payload = {
            "error": {
                "message": f"Gateway provider stream interrupted: {str(e)}",
                "type": "stream_error",
                "code": "stream_interrupted"
            }
        }
        yield f"data: {json.dumps(err_payload)}\n\ndata: [DONE]\n\n".encode("utf-8")

    finally:
        await response.aclose()
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        try:
            async with get_db_connection() as conn:
                await log_request_usage(
                    conn,
                    key_id=key_id,
                    platform=platform,
                    model_id=model_id,
                    tokens_input=input_tokens,
                    tokens_output=output_tokens,
                    duration_ms=duration_ms,
                    ttft_ms=ttft_ms,
                    request_success=stream_success
                )
        except Exception as log_err:
            logger.error(f"[PROXY STREAM LOG ERROR] Failed to log stream metrics: {str(log_err)}")


async def execute_proxy_request(
    payload: ChatCompletionRequest,
    difficulty: Optional[int] = None
) -> Any:
    """
    Manages DB context, BwK solver lookup, provider request building, HTTP execution,
    rate limit auto-tuning, and metric logging.
    """
    payload_dict = payload.model_dump(exclude_none=True)
    est_in_tokens = estimate_prompt_tokens(payload_dict)

    async with get_db_connection() as conn:
        for attempt in range(1, MAX_FAILOVER_RETRIES + 1):
            logger.info(f"[PROXY ATTEMPT {attempt}/{MAX_FAILOVER_RETRIES}] Evaluating route for model='{payload.model}'...")

            route = await select_model_and_platform(conn, payload.model, difficulty, estimated_prompt_tokens=est_in_tokens)

            if not route:
                logger.warning(f"[PROXY FAILOVER] No healthy available route found for '{payload.model}' on attempt {attempt}.")
                if attempt == 1:
                    status = 429 if payload.model == "auto" else 404
                    return JSONResponse(
                        status_code=status,
                        content={"error": {"message": f"No available route found for model '{payload.model}'", "type": "route_not_found"}}
                    )
                else:
                    break

            platform = route["platform"]
            model_id = route["model_id"]
            key_id = route["key_id"]

            creds = await get_api_keys(conn, platform)
            if not creds or not creds.get("api_url"):
                logger.error(f"[PROXY ERROR] Platform '{platform}' credentials or api_url missing.")
                await disable_key(conn, key_id)
                continue

            adapter = get_provider(platform)
            target_url, headers, outbound_body = adapter.build_request(
                creds["api_url"], creds["api_key"], model_id, payload_dict
            )

            start_time = time.perf_counter()
            is_streaming = payload_dict.get("stream") is True or "streamGenerateContent" in target_url

            try:
                if is_streaming:
                    client = httpx.AsyncClient(timeout=CLIENT_TIMEOUT)
                    req = client.build_request("POST", target_url, headers=headers, json=outbound_body)
                    resp = await client.send(req, stream=True)

                    if resp.status_code == 200:
                        logger.info(f"[PROXY SUCCESS 200] Streaming started from {platform}/{model_id}")
                        limits = adapter.extract_quota_limits(platform, resp.status_code, resp.headers)
                        await update_model_limits(conn, platform, model_id, limits["rpm_limit"], limits["rpd_limit"], limits["tpm_limit"], limits["tpd_limit"])

                        response_headers = {
                            "X-Routed-Via": f"{platform}/{model_id}",
                            "Content-Type": "text/event-stream"
                        }
                        return StreamingResponse(
                            stream_generator(resp, key_id, platform, model_id, start_time, adapter, initial_input_tokens=est_in_tokens),
                            headers=response_headers,
                            media_type="text/event-stream"
                        )
                    else:
                        error_body = await resp.aread()
                        await resp.aclose()
                        await client.aclose()
                        duration_ms = int((time.perf_counter() - start_time) * 1000)

                        logger.warning(f"[PROXY HTTP {resp.status_code}] {platform}/{model_id} error: {error_body.decode(errors='ignore')}")

                        if resp.status_code in (429, 413):
                            limits = adapter.extract_quota_limits(platform, resp.status_code, resp.headers, error_body)
                            await update_model_limits(conn, platform, model_id, limits["rpm_limit"], limits["rpd_limit"], limits["tpm_limit"], limits["tpd_limit"])
                            await log_request_usage(conn, key_id, platform, model_id, 0, 0, duration_ms, request_success=False)
                            await add_cooldown(conn, platform, model_id, key_id, 60, route.get("shared_quota_group"))
                            continue
                        elif resp.status_code in (401, 403):
                            await disable_key(conn, key_id)
                            continue
                        elif resp.status_code == 404 or "permission" in error_body.decode(errors="ignore").lower():
                            await mark_model_incapable(conn, key_id, model_id)
                            continue
                        else:
                            await log_request_usage(conn, key_id, platform, model_id, 0, 0, duration_ms, request_success=False)
                            await add_cooldown(conn, platform, model_id, key_id, 30, route.get("shared_quota_group"))
                            continue

                else:
                    async with httpx.AsyncClient(timeout=CLIENT_TIMEOUT) as client:
                        response = await client.post(target_url, headers=headers, json=outbound_body)
                        duration_ms = int((time.perf_counter() - start_time) * 1000)

                        if response.status_code == 200:
                            logger.info(f"[PROXY SUCCESS 200] Non-streaming response from {platform}/{model_id} ({duration_ms}ms)")
                            limits = adapter.extract_quota_limits(platform, response.status_code, response.headers)
                            await update_model_limits(conn, platform, model_id, limits["rpm_limit"], limits["rpd_limit"], limits["tpm_limit"], limits["tpd_limit"])

                            res_data = response.json()
                            normalized_data = adapter.parse_response(res_data)

                            # Extract usage metrics
                            usage = normalized_data.get("usage", {})
                            tokens_in = usage.get("prompt_tokens", 0)
                            tokens_out = usage.get("completion_tokens", 0)

                            await log_request_usage(conn, key_id, platform, model_id, tokens_in, tokens_out, duration_ms, request_success=True)

                            response_headers = {"X-Routed-Via": f"{platform}/{model_id}"}
                            return JSONResponse(content=normalized_data, headers=response_headers)
                        else:
                            logger.warning(f"[PROXY HTTP {response.status_code}] {platform}/{model_id} error: {response.text}")

                            if response.status_code in (429, 413):
                                limits = adapter.extract_quota_limits(platform, response.status_code, response.headers, response.content)
                                await update_model_limits(conn, platform, model_id, limits["rpm_limit"], limits["rpd_limit"], limits["tpm_limit"], limits["tpd_limit"])
                                await log_request_usage(conn, key_id, platform, model_id, 0, 0, duration_ms, request_success=False)
                                await add_cooldown(conn, platform, model_id, key_id, 60, route.get("shared_quota_group"))
                                continue
                            elif response.status_code in (401, 403):
                                await disable_key(conn, key_id)
                                continue
                            elif response.status_code == 404 or "permission" in response.text.lower():
                                await mark_model_incapable(conn, key_id, model_id)
                                continue
                            else:
                                await log_request_usage(conn, key_id, platform, model_id, 0, 0, duration_ms, request_success=False)
                                await add_cooldown(conn, platform, model_id, key_id, 30, route.get("shared_quota_group"))
                                continue

            except Exception as e:
                duration_ms = int((time.perf_counter() - start_time) * 1000)
                logger.error(f"[PROXY EXECUTION EXCEPTION] Attempt {attempt} failed on {platform}/{model_id}: {str(e)}")
                await log_request_usage(conn, key_id, platform, model_id, 0, 0, duration_ms, request_success=False)
                await add_cooldown(conn, platform, model_id, key_id, 30, route.get("shared_quota_group"))
                continue

        logger.error(f"[PROXY EXHAUSTED] All {MAX_FAILOVER_RETRIES} failover attempts failed.")
        return JSONResponse(
            status_code=502,
            content={"error": {"message": "All failover routes were exhausted or returned errors.", "type": "bad_gateway"}}
        )
