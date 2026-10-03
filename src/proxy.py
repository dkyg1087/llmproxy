import uuid
import time
import json
import httpx
import asyncio
from typing import Optional, Dict, Any, AsyncGenerator, Set, Tuple, List
from fastapi.responses import StreamingResponse, JSONResponse

from src.config import logger, MAX_FAILOVER_RETRIES, CLIENT_TIMEOUT
from src.models import ChatCompletionRequest


class TrackedStreamingResponse(StreamingResponse):
    """
    Subclasses Starlette's StreamingResponse to guarantee that the underlying
    generator iterator is always closed cleanly via aclose() upon client disconnect
    or response completion.
    """
    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            if hasattr(self.body_iterator, "aclose"):
                try:
                    await self.body_iterator.aclose()
                except Exception:
                    pass

from src.db import (
    get_decrypted_key,
    log_request_usage,
    add_cooldown,
    update_model_limits,
    set_key_health,
    set_model_health,
    log_request_trace,
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
    platform: str,
    model_id: str,
    start_time: float,
    adapter: Any,
    initial_input_tokens: int = 0,
    request_id: Optional[str] = None,
    difficulty: Optional[int] = None,
    has_tools: bool = False,
    attempt_logs: Optional[List[Dict[str, Any]]] = None,
    client: Optional[httpx.AsyncClient] = None
) -> AsyncGenerator[bytes, None]:
    """
    Decoupled Producer-Consumer stream generator:
    - Producer task reads upstream provider chunks, calculates TTFT and token usage,
      reconstructs full tool-call payloads, and persists to SQLite (shielded).
    - Consumer generator yields chunks to client via an in-memory queue.
    If the downstream client disconnects early, the consumer exits but the producer
    runs to completion and commits full usage & trace logs to SQLite.
    """
    queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue()
    stream_state: Dict[str, Any] = {
        "stream_id": f"chatcmpl-{uuid.uuid4().hex[:16]}",
        "created": int(time.time()),
        "model_id": model_id,
        "role_sent": False,
        "has_tool_calls": False,
        "tool_call_index": 0,
        "done_sent": False,
        "buffer": ""
    }

    async def producer_task():
        ttft_ms: Optional[int] = None
        input_tokens = initial_input_tokens
        output_tokens = 0
        stream_success = True
        error_type: Optional[str] = None
        stream_error_msg: Optional[str] = None
        preview_parts: List[str] = []
        preview_len = 0
        preview_buffer = ""
        tool_calls_acc: Dict[int, Dict[str, str]] = {}

        try:
            async for raw_chunk in response.aiter_bytes():
                if ttft_ms is None:
                    ttft_ms = int((time.perf_counter() - start_time) * 1000)
                    logger.info(f"[PROXY STREAM] First token received (TTFT: {ttft_ms}ms) for {platform}/{model_id}")

                res = adapter.format_stream_chunk(raw_chunk, stream_state=stream_state)
                formatted_chunk, usage_info = res if isinstance(res, tuple) else (res, {})

                if formatted_chunk:
                    await queue.put(formatted_chunk)
                    try:
                        preview_buffer += formatted_chunk.decode("utf-8", errors="ignore")
                        while "\n" in preview_buffer:
                            line, preview_buffer = preview_buffer.split("\n", 1)
                            line_s = line.strip()
                            if line_s.startswith("data:") and line_s != "data: [DONE]":
                                chunk_json = json.loads(line_s[5:].strip())
                                choice = (chunk_json.get("choices") or [{}])[0]
                                delta = choice.get("delta") or {}
                                content = delta.get("content")
                                reasoning = delta.get("reasoning_content") or delta.get("thought") or delta.get("reasoning")
                                if content:
                                    if preview_len < 500:
                                        preview_parts.append(content)
                                        preview_len += len(content)
                                elif reasoning and preview_len < 250:
                                    preview_parts.append(reasoning)
                                    preview_len += len(reasoning)

                                tcs = delta.get("tool_calls")
                                if tcs:
                                    stream_state["has_tool_calls"] = True
                                    for tc in tcs:
                                        idx = tc.get("index", 0)
                                        if idx not in tool_calls_acc:
                                            tool_calls_acc[idx] = {"name": "", "args": ""}
                                        fn = tc.get("function") or {}
                                        if fn.get("name"):
                                            tool_calls_acc[idx]["name"] += fn["name"]
                                        if fn.get("arguments"):
                                            tool_calls_acc[idx]["args"] += fn["arguments"]
                    except Exception:
                        pass

                if usage_info.get("prompt_tokens") is not None:
                    input_tokens = usage_info["prompt_tokens"]
                if usage_info.get("completion_tokens") is not None:
                    output_tokens = usage_info["completion_tokens"]
                elif usage_info.get("delta_chars"):
                    output_tokens += max(1, usage_info["delta_chars"] // 4)

            # Flush any trailing buffer if provider closed stream without trailing newline
            if stream_state.get("buffer"):
                res = adapter.format_stream_chunk(b"\n", stream_state=stream_state)
                formatted_chunk, usage_info = res if isinstance(res, tuple) else (res, {})
                if formatted_chunk:
                    await queue.put(formatted_chunk)
                if usage_info.get("prompt_tokens") is not None:
                    input_tokens = usage_info["prompt_tokens"]
                if usage_info.get("completion_tokens") is not None:
                    output_tokens = usage_info["completion_tokens"]
                elif usage_info.get("delta_chars"):
                    output_tokens += max(1, usage_info["delta_chars"] // 4)

            # Guarantee OpenAI [DONE] terminator if provider closed without it
            if stream_success and not stream_state.get("done_sent", False):
                await queue.put(b"data: [DONE]\n\n")
                stream_state["done_sent"] = True

        except Exception as e:
            stream_success = False
            error_type = "stream_interrupted"
            stream_error_msg = f"Mid-stream connection dropped: {str(e)}"
            logger.error(f"[PROXY STREAM ERROR] {stream_error_msg}")
            err_payload = {
                "error": {
                    "message": f"Gateway provider stream interrupted: {str(e)}",
                    "type": "stream_error",
                    "code": "stream_interrupted"
                }
            }
            await queue.put(f"data: {json.dumps(err_payload)}\n\ndata: [DONE]\n\n".encode("utf-8"))
        finally:
            try:
                await response.aclose()
            except Exception:
                pass
            if client is not None:
                try:
                    await client.aclose()
                except Exception:
                    pass

            duration_ms = int((time.perf_counter() - start_time) * 1000)

            # Build rich response preview including tool calls
            final_preview: Optional[str] = None
            if tool_calls_acc:
                tc_snippets = []
                for idx in sorted(tool_calls_acc.keys()):
                    tc_info = tool_calls_acc[idx]
                    tc_name = tc_info["name"] or "tool"
                    tc_args = tc_info["args"][:100]
                    tc_snippets.append(f"[Tool: {tc_name}({tc_args})]")
                final_preview = " ".join(tc_snippets)[:500]
            elif preview_parts:
                final_preview = "".join(preview_parts).strip()[:500]

            finish_reason = "tool_calls" if (stream_state.get("has_tool_calls") or tool_calls_acc) else ("stop" if stream_success else "error")

            try:
                await asyncio.shield(
                    log_request_usage(
                        platform=platform,
                        model_id=model_id,
                        tokens_input=input_tokens,
                        tokens_output=output_tokens,
                        duration_ms=duration_ms,
                        ttft_ms=ttft_ms,
                        request_success=stream_success,
                        error_type=error_type
                    )
                )
            except Exception as log_err:
                logger.error(f"[PROXY STREAM LOG ERROR] Failed to log stream metrics: {str(log_err)}")

            if request_id:
                try:
                    await asyncio.shield(
                        log_request_trace(
                            request_id=request_id,
                            triage_difficulty=difficulty,
                            has_tools=has_tools,
                            final_platform=platform,
                            final_model_id=model_id,
                            final_status=200 if stream_success else 500,
                            finish_reason=finish_reason,
                            tokens_output=output_tokens,
                            response_preview=final_preview or ("[Empty Stream]" if stream_success else "[Stream Failed]"),
                            attempts_detail=json.dumps(attempt_logs or []),
                            error_summary=stream_error_msg
                        )
                    )
                except Exception as trace_err:
                    logger.error(f"[PROXY STREAM TRACE ERROR] Failed to log stream trace: {trace_err}")

            await queue.put(None)

    prod_task = asyncio.create_task(producer_task())

    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item
    finally:
        if not prod_task.done():
            try:
                await asyncio.shield(prod_task)
            except Exception:
                pass



async def execute_proxy_request(
    payload: ChatCompletionRequest,
    difficulty: Optional[int] = None
) -> Any:
    """
    Manages in-memory failover routing, request building, HTTP execution,
    quota updates, and trace logging. Zero database connection locks held.
    """
    payload_dict = payload.model_dump(exclude_none=True)
    est_in_tokens = estimate_prompt_tokens(payload_dict)
    request_id = f"req-{uuid.uuid4().hex[:12]}"
    attempt_logs: List[Dict[str, Any]] = []
    attempted_routes: Set[Tuple[str, str]] = set()

    for attempt in range(1, MAX_FAILOVER_RETRIES + 1):
        logger.info(f"[PROXY ATTEMPT {attempt}/{MAX_FAILOVER_RETRIES}] Evaluating route for model='{payload.model}'...")

        route = await select_model_and_platform(
            requested_model=payload.model,
            difficulty=difficulty,
            estimated_prompt_tokens=est_in_tokens,
            exclude_models=attempted_routes
        )

        if not route:
            logger.warning(f"[PROXY FAILOVER] No available route for '{payload.model}' on attempt {attempt}.")
            if attempt == 1:
                status = 429 if payload.model == "auto" else 404
                err_msg = f"No available route found for model '{payload.model}'"
                attempt_logs.append({"attempt": attempt, "route": None, "status": status, "error": err_msg})
                await log_request_trace(
                    request_id=request_id,
                    triage_difficulty=difficulty,
                    has_tools=bool(payload_dict.get("tools")),
                    final_platform=None,
                    final_model_id=None,
                    final_status=status,
                    finish_reason="rate_limit" if status == 429 else "not_found",
                    tokens_output=0,
                    response_preview=None,
                    attempts_detail=json.dumps(attempt_logs),
                    error_summary=err_msg
                )
                return JSONResponse(
                    status_code=status,
                    content={"error": {"message": err_msg, "type": "route_not_found"}}
                )
            break

        platform = route["platform"]
        model_id = route["model_id"]
        attempted_routes.add((platform, model_id))

        creds = await get_decrypted_key(platform)
        if not creds or not creds.get("api_url"):
            logger.error(f"[PROXY ERROR] Platform '{platform}' credentials or api_url missing.")
            await set_key_health(platform, is_healthy=False)
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
                    await update_model_limits(platform, model_id, limits["rpm_limit"], limits["rpd_limit"], limits["tpm_limit"], limits["tpd_limit"])

                    attempt_logs.append({"attempt": attempt, "route": f"{platform}/{model_id}", "status": 200})

                    response_headers = {
                        "X-Routed-Via": f"{platform}/{model_id}",
                        "Content-Type": "text/event-stream"
                    }
                    return TrackedStreamingResponse(
                        stream_generator(
                            resp,
                            platform,
                            model_id,
                            start_time,
                            adapter,
                            initial_input_tokens=est_in_tokens,
                            request_id=request_id,
                            difficulty=difficulty,
                            has_tools=bool(payload_dict.get("tools")),
                            attempt_logs=attempt_logs,
                            client=client
                        ),
                        headers=response_headers,
                        media_type="text/event-stream"
                    )
                else:
                    error_body = await resp.aread()
                    await resp.aclose()
                    await client.aclose()
                    duration_ms = int((time.perf_counter() - start_time) * 1000)
                    error_text = error_body.decode(errors="ignore")

                    logger.warning(f"[PROXY HTTP {resp.status_code}] {platform}/{model_id} error: {error_text}")
                    attempt_logs.append({"attempt": attempt, "route": f"{platform}/{model_id}", "status": resp.status_code, "error": error_text[:200]})

                    # Rate limit or quota exhausted
                    if resp.status_code in (429, 413):
                        limits = adapter.extract_quota_limits(platform, resp.status_code, resp.headers, error_body)
                        await update_model_limits(platform, model_id, limits["rpm_limit"], limits["rpd_limit"], limits["tpm_limit"], limits["tpd_limit"])
                        await log_request_usage(platform, model_id, 0, 0, duration_ms, request_success=False, error_type="rate_limit")
                        if limits.get("is_daily_exhausted"):
                            logger.warning(f"[PROXY QUOTA] Daily quota exhausted for {platform}/{model_id}. Applying 12-hour quarantine.")
                            await add_cooldown(platform, model_id, fixed_seconds=43200, shared_quota_group=route.get("shared_quota_group"))
                        else:
                            await add_cooldown(platform, model_id, base_seconds=30, shared_quota_group=route.get("shared_quota_group"))
                        continue

                    # Context overflow (do not quarantine model globally)
                    elif resp.status_code == 400 and ("context" in error_text.lower() or "token" in error_text.lower()):
                        await log_request_usage(platform, model_id, 0, 0, duration_ms, request_success=False, error_type="context_overflow")
                        continue

                    # Invalid Key / Auth error
                    elif resp.status_code in (401, 403):
                        await set_key_health(platform, is_healthy=False)
                        await log_request_usage(platform, model_id, 0, 0, duration_ms, request_success=False, error_type="auth_error")
                        continue

                    # Model not supported/found
                    elif resp.status_code == 404 or "permission" in error_text.lower():
                        await set_model_health(platform, model_id, is_healthy=False)
                        await log_request_usage(platform, model_id, 0, 0, duration_ms, request_success=False, error_type="model_not_found")
                        continue

                    # Server error or overload
                    else:
                        err_type = "overloaded" if resp.status_code == 503 else "provider_error"
                        await log_request_usage(platform, model_id, 0, 0, duration_ms, request_success=False, error_type=err_type)
                        await add_cooldown(platform, model_id, base_seconds=30, shared_quota_group=route.get("shared_quota_group"))
                        continue

            else:
                # Non-streaming call
                async with httpx.AsyncClient(timeout=CLIENT_TIMEOUT) as client:
                    response = await client.post(target_url, headers=headers, json=outbound_body)
                    duration_ms = int((time.perf_counter() - start_time) * 1000)

                    if response.status_code == 200:
                        logger.info(f"[PROXY SUCCESS 200] Non-streaming response from {platform}/{model_id} ({duration_ms}ms)")
                        limits = adapter.extract_quota_limits(platform, response.status_code, response.headers)
                        await update_model_limits(platform, model_id, limits["rpm_limit"], limits["rpd_limit"], limits["tpm_limit"], limits["tpd_limit"])

                        res_data = response.json()
                        normalized_data = adapter.parse_response(res_data)

                        usage = normalized_data.get("usage", {})
                        tokens_in = usage.get("prompt_tokens", est_in_tokens)
                        tokens_out = usage.get("completion_tokens", 0)

                        await log_request_usage(platform, model_id, tokens_in, tokens_out, duration_ms, request_success=True)

                        first_choice = normalized_data.get("choices", [{}])[0]
                        finish_reason = first_choice.get("finish_reason", "stop")
                        msg = first_choice.get("message", {})
                        content_preview = str(msg.get("content") or "").strip()
                        if not content_preview and msg.get("tool_calls"):
                            tc_parts = []
                            for tc in msg["tool_calls"]:
                                fn = tc.get("function", {})
                                fn_name = fn.get("name", "tool")
                                fn_args = str(fn.get("arguments", ""))[:100]
                                tc_parts.append(f"[Tool: {fn_name}({fn_args})]")
                            content_preview = " ".join(tc_parts)
                        content_preview = (content_preview[:500] if content_preview else "[Empty Response]")

                        attempt_logs.append({"attempt": attempt, "route": f"{platform}/{model_id}", "status": 200})
                        await log_request_trace(
                            request_id=request_id,
                            triage_difficulty=difficulty,
                            has_tools=bool(payload_dict.get("tools")),
                            final_platform=platform,
                            final_model_id=model_id,
                            final_status=200,
                            finish_reason=finish_reason,
                            tokens_output=tokens_out,
                            response_preview=content_preview,
                            attempts_detail=json.dumps(attempt_logs)
                        )

                        response_headers = {"X-Routed-Via": f"{platform}/{model_id}"}
                        return JSONResponse(content=normalized_data, headers=response_headers)

                    else:
                        error_text = response.text
                        logger.warning(f"[PROXY HTTP {response.status_code}] {platform}/{model_id} error: {error_text}")
                        attempt_logs.append({"attempt": attempt, "route": f"{platform}/{model_id}", "status": response.status_code, "error": error_text[:200]})

                        if response.status_code in (429, 413):
                            limits = adapter.extract_quota_limits(platform, response.status_code, response.headers, response.content)
                            await update_model_limits(platform, model_id, limits["rpm_limit"], limits["rpd_limit"], limits["tpm_limit"], limits["tpd_limit"])
                            await log_request_usage(platform, model_id, 0, 0, duration_ms, request_success=False, error_type="rate_limit")
                            if limits.get("is_daily_exhausted"):
                                logger.warning(f"[PROXY QUOTA] Daily quota exhausted for {platform}/{model_id}. Applying 12-hour quarantine.")
                                await add_cooldown(platform, model_id, fixed_seconds=43200, shared_quota_group=route.get("shared_quota_group"))
                            else:
                                await add_cooldown(platform, model_id, base_seconds=30, shared_quota_group=route.get("shared_quota_group"))
                            continue
                        elif response.status_code == 400 and ("context" in error_text.lower() or "token" in error_text.lower()):
                            await log_request_usage(platform, model_id, 0, 0, duration_ms, request_success=False, error_type="context_overflow")
                            continue
                        elif response.status_code in (401, 403):
                            await set_key_health(platform, is_healthy=False)
                            await log_request_usage(platform, model_id, 0, 0, duration_ms, request_success=False, error_type="auth_error")
                            continue
                        elif response.status_code == 404 or "permission" in error_text.lower():
                            await set_model_health(platform, model_id, is_healthy=False)
                            await log_request_usage(platform, model_id, 0, 0, duration_ms, request_success=False, error_type="model_not_found")
                            continue
                        else:
                            err_type = "overloaded" if response.status_code == 503 else "provider_error"
                            await log_request_usage(platform, model_id, 0, 0, duration_ms, request_success=False, error_type=err_type)
                            await add_cooldown(platform, model_id, base_seconds=30, shared_quota_group=route.get("shared_quota_group"))
                            continue

        except httpx.TimeoutException as te:
            duration_ms = int((time.perf_counter() - start_time) * 1000)
            logger.error(f"[PROXY TIMEOUT] Attempt {attempt} timed out on {platform}/{model_id}: {te}")
            attempt_logs.append({"attempt": attempt, "route": f"{platform}/{model_id}", "status": 408, "error": "timeout"})
            await log_request_usage(platform, model_id, 0, 0, duration_ms, request_success=False, error_type="timeout")
            await add_cooldown(platform, model_id, base_seconds=30, shared_quota_group=route.get("shared_quota_group"))
            continue

        except Exception as e:
            duration_ms = int((time.perf_counter() - start_time) * 1000)
            logger.error(f"[PROXY EXECUTION EXCEPTION] Attempt {attempt} failed on {platform}/{model_id}: {e}")
            attempt_logs.append({"attempt": attempt, "route": f"{platform}/{model_id}", "status": 500, "error": str(e)})
            await log_request_usage(platform, model_id, 0, 0, duration_ms, request_success=False, error_type="provider_error")
            await add_cooldown(platform, model_id, base_seconds=30, shared_quota_group=route.get("shared_quota_group"))
            continue

    logger.error(f"[PROXY EXHAUSTED] All {MAX_FAILOVER_RETRIES} failover attempts failed.")
    await log_request_trace(
        request_id=request_id,
        triage_difficulty=difficulty,
        has_tools=bool(payload_dict.get("tools")),
        final_platform=None,
        final_model_id=None,
        final_status=502,
        finish_reason="exhausted",
        tokens_output=0,
        response_preview=None,
        attempts_detail=json.dumps(attempt_logs),
        error_summary="All failover routes were exhausted or returned errors."
    )
    return JSONResponse(
        status_code=502,
        content={"error": {"message": "All failover routes were exhausted or returned errors.", "type": "bad_gateway"}}
    )

