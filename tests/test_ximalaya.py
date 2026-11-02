"""喜马拉雅模块可离线测试的部分：URL 判定、上传器重试/串行逻辑（mock 浏览器操作）。"""
from __future__ import annotations

import pytest

from app.config import Config
from app.ximalaya.login import LoginSession
from app.ximalaya.uploader import NotLoggedInError, PlaywrightUploader, looks_like_login_url


def test_looks_like_login_url():
    assert looks_like_login_url("https://passport.ximalaya.com/pages/login/index")
    assert looks_like_login_url("https://www.ximalaya.com/login")
    assert not looks_like_login_url("https://www.ximalaya.com/reform-upload/page/webCenter/upload")
    assert not looks_like_login_url("")


def test_login_session_status_shape(tmp_path):
    s = LoginSession()
    st = s.status()
    assert st == {"running": False, "logged_in": False, "message": "", "qr": None}
    s.shot_path = tmp_path / "qr.png"
    s.shot_path.write_bytes(b"png")
    assert s.status()["qr"] == "/login_qr.png"


@pytest.mark.asyncio
async def test_upload_retry_once_then_success(monkeypatch, tmp_path):
    up = PlaywrightUploader(Config())
    calls = {"n": 0}

    async def fake_upload_once(audio, title, author):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("浏览器崩溃（模拟）")
        return "https://www.ximalaya.com/manage/1"

    async def fake_restart():
        calls["restart"] = calls.get("restart", 0) + 1

    monkeypatch.setattr(up, "_upload_once", fake_upload_once)
    monkeypatch.setattr(up, "_restart", fake_restart)
    url = await up.upload("a.mp3", "t", "au")
    assert url.endswith("/manage/1")
    assert calls["n"] == 2 and calls.get("restart") == 1


@pytest.mark.asyncio
async def test_upload_not_logged_in_no_retry(monkeypatch):
    up = PlaywrightUploader(Config())
    calls = {"n": 0}

    async def fake_upload_once(audio, title, author):
        calls["n"] += 1
        raise NotLoggedInError("未登录")

    async def fake_restart():
        raise AssertionError("NotLoggedIn 不应触发重启重试")

    monkeypatch.setattr(up, "_upload_once", fake_upload_once)
    monkeypatch.setattr(up, "_restart", fake_restart)
    with pytest.raises(NotLoggedInError):
        await up.upload("a.mp3", "t", "au")
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_upload_serialized(monkeypatch):
    """串行锁：两次并发 upload 应先后执行而非交错。"""
    up = PlaywrightUploader(Config())
    active = {"now": 0, "max": 0}

    async def fake_upload_once(audio, title, author):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        import asyncio
        await asyncio.sleep(0.05)
        active["now"] -= 1
        return "ok"

    monkeypatch.setattr(up, "_upload_once", fake_upload_once)
    import asyncio
    await asyncio.gather(up.upload("a", "t1", "u"), up.upload("b", "t2", "u"))
    assert active["max"] == 1
