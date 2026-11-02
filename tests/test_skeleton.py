"""骨架测试：config 解析 / db 状态机 / FastAPI 端点（本地无 fastapi 时跳过 API 部分）。"""
import tempfile
from pathlib import Path

import pytest

SEC_UID = "MS4wLjABAAAAabcdefghij0123456789_-abc"
SEC_UID_X = "MS4wLjABAAAAxxxxxxxxxx0123456789_-xyz"

CFG_OK = f"""
poll_interval_minutes: 15
poll_on_start: false
douyin:
  accounts:
    - name: 测试博主
      url: https://www.douyin.com/user/{SEC_UID}
audio:
  format: mp3
  bitrate: 128k
ximalaya:
  album_id: "123456"
"""


# ---------- config ----------
def test_extract_sec_uid_forms():
    from app.config import extract_sec_uid

    assert extract_sec_uid(SEC_UID) == SEC_UID
    assert extract_sec_uid(f"https://www.douyin.com/user/{SEC_UID}") == SEC_UID
    assert extract_sec_uid(f"分享文本 {SEC_UID} 结尾") == SEC_UID
    assert extract_sec_uid("https://v.douyin.com/abc/") is None


def test_load_config_ok(tmp_path):
    from app.config import load_config

    cfg_file = tmp_path / "app.yaml"
    cfg_file.write_text(CFG_OK, encoding="utf-8")
    cfg = load_config(cfg_file)
    assert len(cfg.douyin.accounts) == 1
    assert cfg.douyin.accounts[0].name == "测试博主"
    assert cfg.douyin.accounts[0].sec_uid == SEC_UID
    assert cfg.poll_interval_minutes == 15
    assert cfg.ximalaya.album_id == "123456"


def test_load_config_reject_bad_format(tmp_path):
    from app.config import load_config

    bad = tmp_path / "bad.yaml"
    bad.write_text("audio: {format: flac}", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(bad)


# ---------- db ----------
def test_store_lifecycle(tmp_path):
    from app.db import Store

    store = Store(tmp_path / "t.db")
    store.set_max_attempts(3)
    assert store.upsert_discovered(
        {"aweme_id": "1", "account": "a", "title": "t1", "author": "u",
         "publish_time": 100, "meta": {"play_url": "http://x"}}
    )
    assert not store.upsert_discovered({"aweme_id": "1", "title": "t1"})
    rec = store.get("1")
    assert rec["meta"] == {"play_url": "http://x"}
    assert rec["status"] == "discovered"

    store.update("1", status="downloaded", video_path="/x/1.mp4", attempts=1)
    store.upsert_discovered({"aweme_id": "2", "account": "a", "title": "t2"})
    store.fail("2", "boom")
    ids = {p["aweme_id"] for p in
           store.pending(("discovered", "downloaded", "converted"))}
    assert ids == {"1", "2"}  # failed 且 attempts<max 也要可重试
    assert store.stats().get("downloaded") == 1
    store.close()


# ---------- FastAPI 组装 ----------
def test_api_endpoints(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")  # 本地无 fastapi 环境时跳过，CI 全量跑
    from fastapi.testclient import TestClient

    cfg_file = tmp_path / "app.yaml"
    cfg_file.write_text(CFG_OK, encoding="utf-8")
    monkeypatch.setenv("SY_CONFIG", str(cfg_file))
    monkeypatch.setenv("SY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SY_MEDIA_DIR", str(tmp_path / "media"))

    import app.main as main_mod

    class OfflineMonitor:  # 测试不触外网：返回空作品列表
        def __init__(self, cfg=None):
            pass

        async def list_posts(self, sec_uid, limit, fetch_all=False):
            return []

    monkeypatch.setattr(main_mod, "F2Monitor", OfflineMonitor)

    from app.main import app

    with TestClient(app) as client:
        r = client.get("/api/health")
        assert r.status_code == 200 and r.json()["status"] == "ok"

        r = client.get("/api/records")
        assert r.status_code == 200 and "records" in r.json()

        r = client.get("/api/config")
        assert r.status_code == 200
        assert r.json()["ximalaya"]["album_id"] == "123456"

        r = client.post("/api/poll")
        assert r.status_code == 200, r.text
        acct = r.json()["accounts"][0]  # OfflineMonitor：0 条作品、0 新增、无异常
        assert acct["new"] == 0 and "error" not in acct

        # 监控账号管理：config 账号已在 lifespan 同步入库
        r = client.get("/api/accounts")
        assert r.status_code == 200
        accts = r.json()["accounts"]
        assert any(a["source"] == "config" for a in accts)

        r = client.post("/api/accounts", json={"sec_uid": SEC_UID_X, "name": "搜索添加"})
        assert r.status_code == 200 and r.json()["ok"] is True
        r = client.get("/api/accounts")
        assert any(a["name"] == "搜索添加" and a["source"] == "search"
                   for a in r.json()["accounts"])

        # config 来源不可删除
        cfg_sec = accts[0]["sec_uid"]
        r = client.delete(f"/api/accounts/{cfg_sec}")
        assert r.status_code == 403
