from fastapi.testclient import TestClient
from main import app
from orchestrator import orchestrator

def test_full_api():
    orchestrator.load_rooms("rooms.yaml")
    
    with TestClient(app) as client:
        # 1. Health check
        res = client.get("/health")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "healthy"
        print("[OK] /health exitoso:", data)

        # 2. Página index
        res_index = client.get("/")
        assert res_index.status_code == 200
        assert "NejoyT Subtítulos" in res_index.text
        print("[OK] GET / (index.html) exitoso")

        # 3. Vista de sala y overlay
        res_room = client.get("/room/auditorio-principal")
        assert res_room.status_code == 200
        assert "subtitles-container" in res_room.text
        print("[OK] GET /room/auditorio-principal exitoso")

        res_overlay = client.get("/room/auditorio-principal?overlay=1")
        assert res_overlay.status_code == 200
        print("[OK] GET /room/auditorio-principal?overlay=1 exitoso")

        # 4. Listado de salas API
        res_rooms = client.get("/api/rooms")
        assert res_rooms.status_code == 200
        rooms_json = res_rooms.json()
        assert "rooms" in rooms_json
        assert len(rooms_json["rooms"]) >= 2
        print(f"[OK] GET /api/rooms exitoso: {len(rooms_json['rooms'])} salas configuradas")

        # 5. Export SRT y VTT
        res_srt = client.get("/api/rooms/auditorio-principal/export?format=srt")
        assert res_srt.status_code == 200
        assert "attachment" in res_srt.headers["content-disposition"]
        print("[OK] GET /api/rooms/{id}/export?format=srt exitoso")

        res_vtt = client.get("/api/rooms/auditorio-principal/export?format=vtt")
        assert res_vtt.status_code == 200
        assert "attachment" in res_vtt.headers["content-disposition"]
        print("[OK] GET /api/rooms/{id}/export?format=vtt exitoso")

        # 6. WebSocket
        with client.websocket_connect("/ws/auditorio-principal?lang=es") as websocket:
            print("[OK] WebSocket /ws/auditorio-principal conectado exitosamente")

    print("\n[OK] TODAS LAS RUTAS Y WEBSOCKETS VERIFICADOS CON EXITO.")

if __name__ == "__main__":
    test_full_api()
