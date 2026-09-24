import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from main import app
from orchestrator import orchestrator

def test_full_api():
    orchestrator.load_rooms("rooms.yaml")
    
    with TestClient(app) as client:
        # 1. Health check & Hardware
        res = client.get("/health")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "healthy"
        assert "hardware" in data
        print("[OK] /health exitoso:", data)

        res_hw = client.get("/api/hardware")
        assert res_hw.status_code == 200
        hw_data = res_hw.json()
        assert "device" in hw_data
        print("[OK] /api/hardware exitoso:", hw_data)

        # 2. Página index y admin
        res_index = client.get("/")
        assert res_index.status_code == 200
        assert "NejoyT Subtítulos" in res_index.text
        print("[OK] GET / (index.html) exitoso")

        res_admin = client.get("/admin")
        assert res_admin.status_code == 200
        assert "NejoyT Control Hub" in res_admin.text
        print("[OK] GET /admin (admin.html) exitoso")

        # 3. Crear sala de prueba dinámicamente vía API
        test_room_payload = {
            "id": "sala-test",
            "name": "Sala de Prueba Dinámica",
            "kind": "file",
            "backend": "local",
            "source_uri": "samples/test_sine.wav",
            "source_lang": "auto",
            "target_lang": "es",
            "target_langs": ["es", "en"],
            "custom_vocabulary": ["Nerdearla"],
            "loop": False,
            "auto_start": False
        }
        res_create = client.post(
            "/api/rooms",
            json=test_room_payload,
            headers={"X-Admin-Token": "nerdearla2026"}
        )
        assert res_create.status_code == 200
        print("[OK] POST /api/rooms exitoso (creación dinámica sin salas precargadas)")

        # 4. Listado de salas API
        res_rooms = client.get("/api/rooms")
        assert res_rooms.status_code == 200
        rooms_json = res_rooms.json()
        assert "rooms" in rooms_json
        assert any(r["id"] == "sala-test" for r in rooms_json["rooms"])
        print(f"[OK] GET /api/rooms exitoso: sala-test verificada")

        # 5. Vista de sala y overlay
        res_room = client.get("/room/sala-test")
        assert res_room.status_code == 200
        assert "subtitles-container" in res_room.text
        print("[OK] GET /room/sala-test exitoso")

        res_overlay = client.get("/room/sala-test?overlay=1")
        assert res_overlay.status_code == 200
        print("[OK] GET /room/sala-test?overlay=1 exitoso")

        # 6. Export SRT y VTT
        res_srt = client.get("/api/rooms/sala-test/export?format=srt")
        assert res_srt.status_code == 200
        assert "attachment" in res_srt.headers["content-disposition"]
        print("[OK] GET /api/rooms/{id}/export?format=srt exitoso")

        res_vtt = client.get("/api/rooms/sala-test/export?format=vtt")
        assert res_vtt.status_code == 200
        assert "attachment" in res_vtt.headers["content-disposition"]
        print("[OK] GET /api/rooms/{id}/export?format=vtt exitoso")

        # 7. WebSocket
        with client.websocket_connect("/ws/sala-test?lang=es") as websocket:
            print("[OK] WebSocket /ws/sala-test conectado exitosamente")

        # 8. Limpiar sala de prueba
        res_del = client.delete(
            "/api/rooms/sala-test",
            headers={"X-Admin-Token": "nerdearla2026"}
        )
        assert res_del.status_code == 200
        print("[OK] DELETE /api/rooms/sala-test exitoso (sistema limpio)")

    # Limpiar rooms.yaml para que quede vacío
    with open("rooms.yaml", "w", encoding="utf-8") as f:
        f.write("rooms: []\n")

    print("\n[OK] TODAS LAS RUTAS Y WEBSOCKETS VERIFICADOS CON EXITO.")

if __name__ == "__main__":
    test_full_api()
