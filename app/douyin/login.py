"""抖音扫码登录：headless 打开抖音首页，弹出登录二维码并轮询截图。

登录态存 data/douyin_profile/（Playwright persistent context）；
成功后导出 cookie 到 data/douyin_cookie.txt，供 f2 接口通道（httpx）提额使用。
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from ..config import Config
from ..ximalaya.login import (
    DEFAULT_TIMEOUT,
    LoginSession,
    _read_account,
    _shot_qr_only,
)

log = logging.getLogger("douyin.login")

DOUYIN_HOME = "https://www.douyin.com/"

LOGIN_BUTTON_SELECTORS = [
    "[class*='login-button']",
    "button:has-text('登录')",
    "li[class*='login']",
    "[class*='login']",
]

# persistent profile 为单实例：登录与搜索共用 data/douyin_profile，必须串行
browser_lock = asyncio.Lock()


def cookie_string(cookies: list[dict]) -> str:
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


def _has_session(cookies: list[dict]) -> bool:
    return any(c.get("name") in ("sessionid", "sessionid_ss") for c in cookies)


async def run_douyin_login(cfg: Config, session: LoginSession) -> None:
    from playwright.async_api import async_playwright

    session.running = True
    session.started_at = time.time()
    session.message = "正在打开抖音…"
    shot = Path(cfg.data_dir) / "douyin_qr.png"
    session.shot_path = shot
    session.qr_path = "/douyin_qr.png"
    pw = await async_playwright().start()
    ctx = None
    try:
        async with browser_lock:
            ctx = await pw.chromium.launch_persistent_context(
                str((Path(cfg.data_dir) / "douyin_profile").resolve()),
                headless=True,
                viewport={"width": 1280, "height": 860},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
            )
            page = await ctx.new_page()
            await page.goto(DOUIN_HOME, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(2500)

            clicked_login = False
            deadline = time.monotonic() + DEFAULT_TIMEOUT
            while time.monotonic() < deadline:
                cookies = await ctx.cookies()
                if _has_session(cookies):
                    try:  # 导出 cookie 供 f2 通道使用
                        cookie_file = Path(cfg.data_dir) / "douyin_cookie.txt"
                        cookie_file.parent.mkdir(parents=True, exist_ok=True)
                        cookie_file.write_text(cookie_string(cookies), encoding="utf-8")
                        log.info("已导出抖音 cookie（%d 项）", len(cookies))
                    except Exception:
                        log.warning("导出 cookie 失败", exc_info=True)
                    session.logged_in = True
                    session.message = "登录成功"
                    try:
                        name, avatar = await _read_account(page)
                        session.account, session.avatar = name, avatar
                        session.message = f"登录成功：{name}" if name else "登录成功"
                    except Exception:
                        log.debug("读取账号信息失败", exc_info=True)
                    return
                session.message = "等待扫码（二维码见状态页）"
                if not clicked_login:
                    for sel in LOGIN_BUTTON_SELECTORS:
                        try:
                            loc = page.locator(sel).first
                            if await loc.is_visible():
                                await loc.click()
                                clicked_login = True
                                await page.wait_for_timeout(2500)
                                break
                        except Exception:
                            continue
                await _shot_qr_only(page, shot)
                await asyncio.sleep(6)
            session.message = "超时未登录，请重新发起"
    except Exception as e:
        session.message = f"登录辅助异常：{e}"
        log.exception("抖音登录辅助异常")
    finally:
        try:
            if ctx:
                await ctx.close()
            await pw.stop()
        except Exception:
            pass
        session.running = False


def start_douyin_login_task(cfg: Config) -> LoginSession:
    session = LoginSession()
    session._task = asyncio.get_event_loop().create_task(
        run_douyin_login(cfg, session)
    )
    return session
