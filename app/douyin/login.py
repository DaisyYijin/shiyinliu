"""抖音扫码登录：headless 打开抖音登录页，弹出二维码并轮询截图。

抖音反爬比喜马拉雅严格，浏览器必须做反检测处理（正常 Chrome UA、
去除 automation 标志），否则页面可能不渲染登录控件。
登录态存 data/douyin_profile/（Playwright persistent context）；
成功后导出 cookie 到 data/douyin_cookie.txt（f2 接口通道提额）并写
data/douyin_auth.json（重启后状态页恢复「已登录」显示）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from ..config import DEFAULT_DOUYIN_UA, Config
from ..ximalaya.login import (
    DEFAULT_TIMEOUT,
    LoginSession,
    _read_account,
    _shot,
    _shot_qr_only,
)

log = logging.getLogger("douyin.login")

DOUYIN_LOGIN_URL = "https://www.douyin.com/login"
AUTH_FILE = "douyin_auth.json"

LOGIN_BUTTON_SELECTORS = [
    "[class*='login-button']",
    "button:has-text('登录')",
    "div[class*='login']:has-text('登录')",
    "li[class*='login']",
]

# 登录弹窗内切到扫码方式的 tab（存在才点，兼容多种弹窗形态）
QR_TAB_SELECTORS = [
    "[class*='qrcode-tab']",
    "li:has-text('扫码登录')",
    "div:has-text('扫码登录')",
    "text=扫码登录",
]


def write_douyin_auth(cfg: Config, account: str, avatar: str) -> None:
    try:
        p = Path(cfg.data_dir) / AUTH_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({"account": account, "avatar": avatar, "at": int(time.time())},
                       ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        log.warning("写入抖音登录标记失败", exc_info=True)


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
                user_agent=DEFAULT_DOUYIN_UA,  # 覆盖 HeadlessChrome UA（抖音风控点）
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ],
                ignore_default_args=["--enable-automation"],
                viewport={"width": 1280, "height": 860},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
            )
            page = await ctx.new_page()
            await page.goto(DOUIN_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)

            clicked_login = False
            round_no = 0
            deadline = time.monotonic() + DEFAULT_TIMEOUT
            while time.monotonic() < deadline:
                round_no += 1
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
                    name = avatar = ""
                    try:
                        name, avatar = await _read_account(page)
                        session.account, session.avatar = name, avatar
                        session.message = f"登录成功：{name}" if name else "登录成功"
                    except Exception:
                        log.debug("读取账号信息失败", exc_info=True)
                    write_douyin_auth(cfg, name or "", avatar or "")
                    return
                session.message = "等待扫码（二维码见状态页）"
                if not clicked_login:
                    clicked_login = await _try_open_login_dialog(page)
                else:
                    await _try_click_qr_tab(page)  # 确保停在扫码 tab
                qr_ok = await _shot_qr_only(page, shot)
                if not qr_ok and round_no <= 3:
                    # 二维码没截到：整页留档，便于按真实页面修正选择器
                    await _shot(
                        page,
                        Path(cfg.debug_dir) / f"douyin_login_page_{int(time.time())}.png",
                    )
                    session.message = "未找到二维码元素，已留档页面截图（data/debug/）"
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


async def _try_open_login_dialog(page: Any) -> bool:
    """点击页面上的登录入口，打开登录弹窗；成功返回 True。"""
    for sel in LOGIN_BUTTON_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible():
                await loc.click()
                await page.wait_for_timeout(2500)
                await _try_click_qr_tab(page)
                return True
        except Exception:
            continue
    return False


async def _try_click_qr_tab(page: Any) -> None:
    """若登录弹窗内有「扫码登录」tab 则点击（可能默认是验证码登录）。"""
    for sel in QR_TAB_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible():
                await loc.click()
                await page.wait_for_timeout(1200)
                return
        except Exception:
            continue


def start_douyin_login_task(cfg: Config) -> LoginSession:
    session = LoginSession()
    session._task = asyncio.get_event_loop().create_task(
        run_douyin_login(cfg, session)
    )
    return session
