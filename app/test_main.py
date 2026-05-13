import sys
from unittest.mock import MagicMock, patch

# Mock heavy dependencies before importing main
# Why: CI runners don't have PostgreSQL or Redis
# Mocking replaces them with fake objects that
# behave correctly without needing real connections
sys.modules['psycopg2'] = MagicMock()
sys.modules['redis'] = MagicMock()
sys.modules['prometheus_fastapi_instrumentator'] = MagicMock()

with patch('psycopg2.connect'), patch('redis.from_url'):
    from main import app

from fastapi.testclient import TestClient
client = TestClient(app)

def test_root():
    """Confirms service is running and returns branch list"""
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["service"] == "SA Retail Inventory API"
    assert "Soweto" in response.json()["branches"]

def test_health():
    """Confirms health endpoint returns 200"""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"

def test_health_has_timestamp():
    """Confirms health response includes a timestamp"""
    response = client.get("/health")
    assert "timestamp" in response.json()
