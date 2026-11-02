"""扫码登录：headless 打开创作中心，轮询整页截图到 data/login_qr.png 供扫码。

配合状态页 /login 展示：容器无需 GUI，手机扫网页上展示的二维码即可。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config
from .uploader import UPLOAD_PAGE, _shot, looks_like_login_url

log = logging.getLogger("ximalaya.login")

REFRESH_SECONDS = 6
DEFAULT_TIMEOUT = 300


@dataclass
class LoginSession:
    running: bool = False
    logged_in: bool = False
    message: str = ""
    started_at: float = 0.0
    shot_path: Path | None = None
    _task: Any = field(default=None, repr=False)

    def status(self) -> dict:
        return {
            "running": self.running,
            "logged_in": self.logged_in,
            "message": self.message,
            "qr": ("/login_qr.png" if self.shot_path and self.shot_path.exists() else None),
        }


async def run_login(cfg: Config, session: LoginSession) -> None:
    """循环：打开上传页 -> 截图 -> 检测登录态，直到成功或超时。"""
    from playwright.async_api import async_playwright

    session.running = True
    session.started_at = time.time()
    session.message = "正在打开喜马拉雅…"
    shot = Path(cfg.data_dir) / "login_qr.png"
    session.shot_path = shot
    pw = await async_playwright().start()
    ctx = None
    try:
        ctx = await pw.chromium.launch_persistent_context(
            str(Path(cfg.profile_dir).resolve()),
            headless=True,
            viewport={"width": 1280, "height": 860},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        page = await ctx.new_page()
        deadline = time.monotonic() + DEFAULT_TIMEOUT
        while time.monotonic() < deadline:
            try:
                await page.goto(UPLOAD_PAGE, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(2500)
                if not looks_like_login_url(page.url):
                    session.logged_in = True
                    session.message = "登录成功"
                    return
                session.message = "等待扫码（二维码见状态页）"
                await _shot(page, shot)
            except Exception as e:
                session.message = f"页面异常，重试中：{e}"
            await asyncio.sleep(REFRESH_SECONDS)
        session.message = "超时未登录，请重新发起"
    except Exception as e:
        session.message = f"登录辅助异常：{e}"
        log.exception("登录辅助异常")
    finally:
        try:
            if ctx:
                await ctx.close()
            await pw.stop()
        except Exception:
            pass
        session.running = False


def start_login_task(cfg: Config) -> LoginSession:
    session = LoginSession()
    session._task = asyncio.get_event_loop().create_task(run_login(cfg, session))
    return session
