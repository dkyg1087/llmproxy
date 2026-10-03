import pytest
import asyncio
from fastapi.testclient import TestClient
from src.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_get_models_endpoint(client):
    response = client.get("/v1/models")
    assert response.status_code == 200
    data = response.json()
    assert "data" in data
    model_ids = [m["id"] for m in data["data"]]
    assert model_ids == ["auto"]


def test_direct_triage_endpoint(client):
    payload = {
        "model": "triage",
        "messages": [{"role": "user", "content": "What is the capital of Japan?"}]
    }
    response = client.post("/v1/chat/completions", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert "choices" in data
    content = data["choices"][0]["message"]["content"]
    assert "difficulty" in content

    # Verify triage trace is recorded
    res_traces = client.get("/api/admin/traces?limit=5")
    traces = res_traces.json()
    triage_trace = next((t for t in traces if t.get("final_model_id") == "triage"), None)
    assert triage_trace is not None
    assert triage_trace["final_status"] == 200


def test_admin_dashboard_endpoints(client):
    # 1. UI serving
    res_ui = client.get("/")
    assert res_ui.status_code == 200

    # 2. Stats
    res_stats = client.get("/api/admin/stats")
    assert res_stats.status_code == 200
    data_stats = res_stats.json()
    assert "total_requests" in data_stats
    assert "healthy_models" in data_stats

    # 3. Models list
    res_models = client.get("/api/admin/models")
    assert res_models.status_code == 200
    assert isinstance(res_models.json(), list)

    # 4. Keys list
    res_keys = client.get("/api/admin/keys")
    assert res_keys.status_code == 200
    assert isinstance(res_keys.json(), list)

    # 5. Analytics
    res_analytics = client.get("/api/admin/analytics?timeframe=7d")
    assert res_analytics.status_code == 200
    data_analytics = res_analytics.json()
    assert "total_requests" in data_analytics
    assert "models_breakdown" in data_analytics

    # 6. Debug trace
    res_trace = client.get("/api/debug/trace")
    assert res_trace.status_code in (200, 404)


def test_admin_mutation_endpoints(client):
    # 1. Add model
    res_add = client.post("/api/admin/models/add", json={
        "platform": "google",
        "model_id": "pytest-e2e-model",
        "display_name": "Pytest E2E Model",
        "base_score": 3,
        "rpm_limit": 20
    })
    assert res_add.status_code == 200

    # 2. Update model
    res_upd = client.post("/api/admin/models/update", json={
        "platform": "google",
        "model_id": "pytest-e2e-model-v2",
        "old_platform": "google",
        "old_model_id": "pytest-e2e-model",
        "display_name": "Pytest E2E Model V2",
        "base_score": 4
    })
    assert res_upd.status_code == 200

    # 3. Toggle model
    res_toggle = client.post("/api/admin/models/toggle", json={
        "platform": "google",
        "model_id": "pytest-e2e-model-v2",
        "enabled": False
    })
    assert res_toggle.status_code == 200
    assert res_toggle.json()["enabled"] is False

    # 4. Delete model
    res_del = client.post("/api/admin/models/delete", json={
        "platform": "google",
        "model_id": "pytest-e2e-model-v2"
    })
    assert res_del.status_code == 200

    # 4b. Clear cooldown endpoint
    res_cd = client.post("/api/admin/models/clear-cooldown", json={
        "platform": "google",
        "model_id": "gemini-3-flash-preview"
    })
    assert res_cd.status_code == 200
    assert res_cd.json()["status"] == "ok"

    # 5. Triage settings get and save
    res_triage = client.get("/api/admin/triage")
    assert res_triage.status_code == 200

    res_triage_save = client.post("/api/admin/triage/save", json={
        "triage_strategy": "heuristic",
        "triage_platform": "google",
        "triage_model": "gemini-3.5-flash-lite"
    })
    assert res_triage_save.status_code == 200

    # Revert back
    client.post("/api/admin/triage/save", json={
        "triage_strategy": "llm",
        "triage_platform": "google",
        "triage_model": "gemini-3.5-flash-lite"
    })

    # 6. Traces & Audit endpoints
    res_traces = client.get("/api/admin/traces?limit=10")
    assert res_traces.status_code == 200
    assert isinstance(res_traces.json(), list)

    res_audit = client.get("/api/admin/audit?limit=10")
    assert res_audit.status_code == 200
    assert isinstance(res_audit.json(), list)
    # Check that audit recorded our previous admin actions
    assert any(log.get("action") == "SAVE_TRIAGE_SETTINGS" for log in res_audit.json())


def test_google_stream_packet_reassembly_and_tool_calls():
    from src.providers.google import GoogleAdapter
    import json

    adapter = GoogleAdapter()

    # 1. Test TCP Packet Fragmentation across 3 chunk slices
    state = {
        "stream_id": "test-stream-tcp",
        "created": 1000000,
        "model_id": "gemini-3.6-flash",
        "role_sent": False,
        "has_tool_calls": False,
        "tool_call_index": 0,
        "done_sent": False,
        "buffer": ""
    }

    raw_event = (
        'data: {"candidates": [{"content": {"parts": [{"text": "Hello world!"}],"role": "model"},'
        '"finishReason": "STOP","index": 0}],"usageMetadata": {"promptTokenCount": 20,"candidatesTokenCount": 8}}\n\n'
    ).encode("utf-8")

    slice1 = raw_event[:35]
    slice2 = raw_event[35:80]
    slice3 = raw_event[80:]

    out1, _ = adapter.format_stream_chunk(slice1, stream_state=state)
    assert out1 == b""

    out2, _ = adapter.format_stream_chunk(slice2, stream_state=state)
    assert out2 == b""

    out3, usage = adapter.format_stream_chunk(slice3, stream_state=state)
    assert b"data: " in out3
    assert b"chat.completion.chunk" in out3
    assert b'"finish_reason": "stop"' in out3
    assert b"data: [DONE]\n\n" in out3
    assert state["done_sent"] is True
    assert usage["prompt_tokens"] == 20
    assert usage["completion_tokens"] == 8

    # 2. Test Tool Call Streaming Serialization (0-based index and role: assistant)
    tool_state = {
        "stream_id": "test-stream-tools",
        "created": 1000000,
        "model_id": "gemini-3.6-flash",
        "role_sent": False,
        "has_tool_calls": False,
        "tool_call_index": 0,
        "done_sent": False,
        "buffer": ""
    }

    tool_event = (
        'data: {"candidates": [{"content": {"parts": [{'
        '"functionCall": {"name": "run_command", "args": {"command": "dir"}},'
        '"thoughtSignature": "test_sig_123"'
        '}],"role": "model"},"finishReason": "STOP","index": 0}]}\n\n'
    ).encode("utf-8")

    out_tool, _ = adapter.format_stream_chunk(tool_event, stream_state=tool_state)
    lines = [line.strip() for line in out_tool.decode("utf-8").split("\n\n") if line.strip().startswith("data: {")]
    assert len(lines) == 1
    parsed_chunk = json.loads(lines[0][6:])
    choice = parsed_chunk["choices"][0]
    assert choice["delta"]["role"] == "assistant"
    assert choice["delta"]["tool_calls"][0]["index"] == 0
    assert choice["delta"]["tool_calls"][0]["function"]["name"] == "run_command"
    assert choice["finish_reason"] == "tool_calls"
    assert b"data: [DONE]\n\n" in out_tool


@pytest.mark.asyncio
async def test_stream_generator_trace_logging():
    import time
    from src.proxy import stream_generator
    from src.providers.google import GoogleAdapter
    from src.db.logs import get_recent_traces

    adapter = GoogleAdapter()
    raw_event = (
        'data: {"candidates": [{"content": {"parts": [{"text": "Streaming test snippet"}],"role": "model"},'
        '"finishReason": "STOP","index": 0}],"usageMetadata": {"promptTokenCount": 10,"candidatesTokenCount": 4}}\n\n'
    ).encode("utf-8")

    class MockResponse:
        async def aiter_bytes(self):
            yield raw_event
        async def aclose(self):
            pass

    mock_resp = MockResponse()
    gen = stream_generator(
        response=mock_resp,
        platform="google",
        model_id="gemini-3.5-flash-lite",
        start_time=time.perf_counter(),
        adapter=adapter,
        initial_input_tokens=10,
        request_id="test_req_snippet_123",
        difficulty=2,
        has_tools=False,
        attempt_logs=[{"attempt": 1, "route": "google/gemini-3.5-flash-lite", "status": 200}]
    )

    chunks = [c async for c in gen]
    assert len(chunks) >= 1

    traces = await get_recent_traces(limit=5)
    target = next((t for t in traces if t["request_id"] == "test_req_snippet_123"), None)
    assert target is not None
    assert "Streaming test snippet" in target["response_preview"]
    assert target["tokens_output"] == 4
    assert target["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_stream_terminal_logs_endpoint():
    import json
    from src.config import logger
    from src.main import stream_terminal_logs

    logger.info("Test SSE terminal message")
    response = await stream_terminal_logs()
    assert response.status_code == 200

    from src.config import recent_terminal_logs
    iterator = response.body_iterator
    # Verify the logged message is present in the backlog stream
    found = False
    for _ in range(len(recent_terminal_logs)):
        chunk = await anext(iterator)
        if chunk.startswith("data: "):
            payload = json.loads(chunk[6:])
            if "Test SSE terminal message" in payload.get("line", ""):
                found = True
                break
    assert found is True
    await iterator.aclose()


@pytest.mark.asyncio
async def test_stream_generator_early_disconnect():
    """Verifies that when a client disconnects mid-stream, the producer still logs to DB."""
    import time
    from src.proxy import stream_generator
    from src.providers.google import GoogleAdapter
    from src.db.logs import get_recent_traces

    adapter = GoogleAdapter()
    raw_event_1 = (
        'data: {"candidates": [{"content": {"parts": [{"text": "First chunk"}],"role": "model"},'
        '"index": 0}]}\n\n'
    ).encode("utf-8")
    raw_event_2 = (
        'data: {"candidates": [{"content": {"parts": [{"text": "Second chunk"}],"role": "model"},'
        '"finishReason": "STOP","index": 0}],"usageMetadata": {"promptTokenCount": 15,"candidatesTokenCount": 6}}\n\n'
    ).encode("utf-8")

    class MultiMockResponse:
        async def aiter_bytes(self):
            yield raw_event_1
            await asyncio.sleep(0.05)
            yield raw_event_2
        async def aclose(self):
            pass

    req_id = "test_req_disconnect_456"
    gen = stream_generator(
        response=MultiMockResponse(),
        platform="google",
        model_id="gemini-3.5-flash-lite",
        start_time=time.perf_counter(),
        adapter=adapter,
        initial_input_tokens=15,
        request_id=req_id,
        difficulty=2,
        has_tools=False,
        attempt_logs=[{"attempt": 1, "route": "google/gemini-3.5-flash-lite", "status": 200}]
    )

    # Simulate client breaking immediately after chunk 1
    async for chunk in gen:
        break
    await gen.aclose()

    # Give producer task brief moment to finalize DB write
    await asyncio.sleep(0.1)

    traces = await get_recent_traces(limit=5)
    target = next((t for t in traces if t["request_id"] == req_id), None)
    assert target is not None
    assert target["final_status"] == 200
    assert "First chunk" in target["response_preview"]


@pytest.mark.asyncio
async def test_stream_generator_tool_call_preview():
    """Verifies that streaming tool call names and argument deltas are accumulated into preview."""
    import time
    from src.proxy import stream_generator
    from src.providers.openai_compat import OpenAIAdapter
    from src.db.logs import get_recent_traces

    adapter = OpenAIAdapter()
    # Chunk 1: delivers tool name
    raw_chunk_1 = (
        'data: {"id":"chatcmpl-test","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_abc","type":"function","function":{"name":"read_file","arguments":""}}]},"finish_reason":null}]}\n\n'
    ).encode("utf-8")
    # Chunk 2: delivers arguments
    raw_chunk_2 = (
        'data: {"id":"chatcmpl-test","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"path\\": \\"src/main.py\\"}"}}]},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":20,"completion_tokens":8}}\n\n'
    ).encode("utf-8")
    raw_done = b"data: [DONE]\n\n"

    class ToolMockResponse:
        async def aiter_bytes(self):
            yield raw_chunk_1
            yield raw_chunk_2
            yield raw_done
        async def aclose(self):
            pass

    req_id = "test_req_tool_preview_789"
    gen = stream_generator(
        response=ToolMockResponse(),
        platform="groq",
        model_id="openai/gpt-oss-120b",
        start_time=time.perf_counter(),
        adapter=adapter,
        initial_input_tokens=20,
        request_id=req_id,
        difficulty=3,
        has_tools=True,
        attempt_logs=[{"attempt": 1, "route": "groq/openai/gpt-oss-120b", "status": 200}]
    )

    chunks = [c async for c in gen]
    assert len(chunks) >= 2

    traces = await get_recent_traces(limit=5)
    target = next((t for t in traces if t["request_id"] == req_id), None)
    assert target is not None
    assert target["finish_reason"] == "tool_calls"
    assert target["has_tools"] is True
    assert "[Tool: read_file(" in target["response_preview"]
    assert "src/main.py" in target["response_preview"]




