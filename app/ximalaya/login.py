"""扫码登录：headless 打开创作中心，轮询二维码截图到 data/login_qr.png 供扫码。

二维码优先只截二维码元素本身（多候选选择器，遍历主页面与 iframe），
找不到才回退整页截图；登录成功后从页面读取账号昵称/头像回显到状态页。
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

QR_SELECTORS = [
    "img[class*='qr']",
    "img[src*='qrcode']",
    "img[src*='qrCode']",
    "canvas[class*='qr']",
    "canvas",
    "[class*='qrcode'] img",
    "[class*='QRCode'] img",
    "[class*='qr-code'] img",
    "[class*='qrcode']",
]

ACCOUNT_NAME_SELECTORS = [
    "[class*='nickname']",
    "[class*='userName']",
    "[class*='user-name']",
    "header [class*='name']",
    "[class*='account-name']",
]

AVATAR_SELECTORS = [
    "[class*='avatar'] img",
    "img[class*='avatar']",
    "[class*='Avatar'] img",
]


@dataclass
class LoginSession:
    running: bool = False
    logged_in: bool = False
    message: str = ""
    started_at: float = 0.0
    account: str = ""
    avatar: str = ""
    shot_path: Path | None = None
    _task: Any = field(default=None, repr=False)

    def status(self) -> dict:
        return {
            "running": self.running,
            "logged_in": self.logged_in,
            "message": self.message,
            "account": self.account or None,
            "avatar": self.avatar or None,
            "qr": ("/login_qr.png" if self.shot_path and self.shot_path.exists() else None),
        }


async def _shot_qr_only(page: Any, shot: Path) -> None:
    """优先只截二维码元素（主页面 + iframe 逐帧多候选尝试），失败回退整页。"""
    for frame in page.frames:
        for sel in QR_SELECTORS:
            try:
                loc = frame.locator(sel).first
                if await loc.is_visible():
                    await loc.screenshot(path=str(shot))
                    return
            except Exception:
                continue
    await _shot(page, shot)  # 回退：整页截图


async def _read_account(page: Any) -> tuple[str, str]:
    """尽力从页面读取账号昵称与头像地址，读不到返回空串。"""
    name = avatar = ""
    for sel in ACCOUNT_NAME_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible():
                text = (await loc.inner_text()).strip()
                if text and len(text) <= 30:
                    name = text
                    break
        except Exception:
            continue
    for sel in AVATAR_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible():
                src = await loc.get_attribute("src")
                if src and src.startswith("http"):
                    avatar = src
                    break
        except Exception:
            continue
    return name, avatar


async def run_login(cfg: Config, session: LoginSession) -> None:
    """循环：打开上传页 -> 截取二维码 -> 检测登录态，直到成功或超时。"""
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
                    try:  # 回显账号信息（尽力而为，失败不影响登录态）
                        name, avatar = await _read_account(page)
                        session.account, session.avatar = name, avatar
                        session.message = f"登录成功：{name}" if name else "登录成功"
                    except Exception:
                        log.debug("读取账号信息失败", exc_info=True)
                    return
                session.message = "等待扫码（二维码见状态页）"
                await _shot_qr_only(page, shot)
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
