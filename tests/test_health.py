import asyncio
from fastapi.testclient import TestClient
from main import app

def test_health():
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["service"] == "nerdearla-live-subtitles"
        print("[OK] Test de FastAPI /health exitoso:", data)

if __name__ == "__main__":
    test_health()
