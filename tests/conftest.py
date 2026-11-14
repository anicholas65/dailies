import os
import pytest

os.environ.setdefault("DAILIES_SCHEDULER", "0")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app import config, db
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(config, "DOWNLOADS_DIR", tmp_path / "downloads")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "config" / "dailies.db")
    monkeypatch.setattr(config, "BROWSER_PROFILE", tmp_path / "config" / "browser")
    monkeypatch.setattr(config, "COOKIES_FILE", tmp_path / "config" / "cookies.txt")
    monkeypatch.setattr(config, "SCHEDULER", False)
    db.reset()
    from app import worker
    monkeypatch.setattr(worker, "index_now", lambda sid: None)
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c
    db.reset()
