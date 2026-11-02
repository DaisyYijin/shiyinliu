"""下载器测试：httpx MockTransport，不发真实请求。"""
from __future__ import annotations

import httpx
import pytest

from pathlib import Path

from app.config import Config
from app.douyin.downloader import HttpDownloader


def make_transport(status=200, body=b"x" * 5000, headers=None):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body, headers=headers or {})
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_download_ok(tmp_path):
    d = HttpDownloader(Config())
    path = await d.download("http://fake/play", tmp_path, "42",
                            transport=make_transport())
    assert Path(path).name == "42.mp4"
    assert Path(path).stat().st_size == 5000


@pytest.mark.asyncio
async def test_download_expired_url(tmp_path):
    d = HttpDownloader(Config())
    with pytest.raises(RuntimeError, match="过期"):
        await d.download("http://fake/play", tmp_path, "42",
                         transport=make_transport(status=403))


@pytest.mark.asyncio
async def test_download_tiny_body(tmp_path):
    d = HttpDownloader(Config())
    with pytest.raises(RuntimeError, match="过小"):
        await d.download("http://fake/play", tmp_path, "42",
                         transport=make_transport(body=b"tiny"))
    assert not (tmp_path / "42.mp4").exists()
