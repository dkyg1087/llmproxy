import pytest
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


def test_admin_dashboard_endpoints(client):
    # Test GET /admin UI serving
    res_ui = client.get("/")
    assert res_ui.status_code == 200

    # Test GET /api/admin/stats
    res_stats = client.get("/api/admin/stats")
    assert res_stats.status_code == 200
    data_stats = res_stats.json()
    assert "total_requests" in data_stats
    assert "healthy_models" in data_stats

    # Test GET /api/admin/models
    res_models = client.get("/api/admin/models")
    assert res_models.status_code == 200
    data_models = res_models.json()
    assert isinstance(data_models, list)

    # Test GET /api/admin/keys
    res_keys = client.get("/api/admin/keys")
    assert res_keys.status_code == 200
    data_keys = res_keys.json()
    assert isinstance(data_keys, list)

    # Test GET /api/admin/analytics
    res_analytics = client.get("/api/admin/analytics?timeframe=7d")
    assert res_analytics.status_code == 200
    data_analytics = res_analytics.json()
    assert "total_requests" in data_analytics
    assert "total_tokens" in data_analytics
    assert "models_breakdown" in data_analytics
