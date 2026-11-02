"""无水印视频下载：httpx 流式下载（play_url 会 302 到真实 CDN 地址）。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from ..config import Config, DEFAULT_DOUYIN_UA

log = logging.getLogger("douyin.downloader")


class HttpDownloader:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._headers = {
            "User-Agent": DEFAULT_DOUYIN_UA,
            "Referer": "https://www.douyin.com/",
        }

    async def download(
        self,
        play_url: str,
        dest_dir: Path | str,
        aweme_id: str,
        *,
        transport: Any = None,
    ) -> str:
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{aweme_id}.mp4"
        timeout = httpx.Timeout(30, read=120)
        async with httpx.AsyncClient(
            headers=self._headers, follow_redirects=True, timeout=timeout,
            transport=transport,
        ) as client:
            async with client.stream("GET", play_url) as resp:
                if resp.status_code in (403, 410):
                    raise RuntimeError(f"播放地址已过期（HTTP {resp.status_code}），需重新获取")
                resp.raise_for_status()
                tmp = dest.with_suffix(".part")
                with tmp.open("wb") as f:
                    async for chunk in resp.aiter_bytes(1 << 16):
                        f.write(chunk)
        if not tmp.exists() or tmp.stat().st_size < 1024:
            raise RuntimeError("下载内容过小，疑似失败")
        tmp.replace(dest)
        log.info("已下载 %s（%.1f MB）", dest.name, dest.stat().st_size / 1048576)
        return str(dest)
