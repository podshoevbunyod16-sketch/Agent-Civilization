from fastapi.testclient import TestClient

from app.main import create_app


def test_production_spa_serves_client_routes_and_preserves_api_404(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<!doctype html><title>demo spa</title>")
    monkeypatch.setenv("FRONTEND_DIST_DIR", str(tmp_path))
    app = create_app()

    with TestClient(app) as client:
        assert "demo spa" in client.get("/").text
        assert "demo spa" in client.get("/plans").text
        assert client.get("/api/v1/not-a-real-route").status_code == 404
