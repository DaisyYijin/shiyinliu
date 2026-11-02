"""抖音扫码登录：headless 打开抖音首页（未登录会自动弹出扫码登录框）。

真实页面结构（2026-11 核实）：弹窗内二维码为 <img alt="二维码">，
src 为 base64 PNG —— 首选直接提取 base64 写文件（零截图失真），
元素截图 / 整页截图逐级兜底。
登录态存 data/douyin_profile/；成功后导出 cookie 到 data/douyin_cookie.txt
（f2 接口通道提额）并写 data/douyin_auth.json（重启后恢复「已登录」显示）。
"""
from __future__ import annotations

import asyncio
import base64
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

DOUYIN_HOME_URL = "https://www.douyin.com/"
AUTH_FILE = "douyin_auth.json"
REFRESH_SECONDS = 8  # 轮询间隔：兼顾二维码刷新与降低访问频率（防风控）

# 顶栏登录入口（弹窗未自动弹出时兜底）
LOGIN_BUTTON_SELECTORS = [
    "button:has-text('登录')",
    'text="登录"',
    "[class*='login-button']",
    "li[class*='login']",
]

# 登录弹窗内切到扫码方式的 tab（存在才点）
QR_TAB_SELECTORS = [
    "[class*='qrcode-tab']",
    "li:has-text('扫码登录')",
    "text=扫码登录",
]

# 扫码确认后的二次验证表单（抖音新设备登录常见：短信验证码 / 手机尾号）
VERIFY_INPUT_SELECTORS = [
    "input[placeholder*='手机']",
    "input[placeholder*='验证码']",
    "input[placeholder*='尾号']",
]
VERIFY_SUBMIT_SELECTORS = [
    "button:has-text('确认')",
    "button:has-text('验证')",
    "button:has-text('提交')",
    "button:has-text('登录')",
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
            deadline = time.monotonic() + DEFAULT_TIMEOUT
            need_goto = True
            last_goto = 0.0
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
                    name = avatar = ""
                    try:
                        name, avatar = await _read_account(page)
                        session.account, session.avatar = name, avatar
                        session.message = f"登录成功：{name}" if name else "登录成功"
                    except Exception:
                        log.debug("读取账号信息失败", exc_info=True)
                    write_douyin_auth(cfg, name or "", avatar or "")
                    return

                # 按需（重）加载页面：仅当从未加载、验证超时或页面完全无进展。
                # 关键：二维码存在期间绝不重载——扫码确认后页面会自然切换到
                # 验证表单或登录成功，盲目重载会把这一步刷掉（表现为永远
                # 停留在"等待扫码"）。
                if need_goto:
                    try:
                        await page.goto(DOUYIN_HOME_URL, wait_until="domcontentloaded",
                                        timeout=60000)
                        await page.wait_for_timeout(3000)
                    except Exception as e:
                        session.message = f"页面加载异常，重试中：{e}"
                        await asyncio.sleep(REFRESH_SECONDS)
                        continue
                    last_goto = time.monotonic()
                    need_goto = False

                # 扫码确认后可能出现二次验证（短信验证码/手机尾号）：
                # 展示验证页并等待用户在状态页输入，代填提交
                vloc, vhint = await _detect_verify_input(page)
                if vloc is not None:
                    session.request_verify(f"扫码已确认，需二次验证：{vhint}")
                    await _shot(page, shot)  # 展示验证页面上下文
                    value = await session.wait_verify(timeout=180)
                    session.pending_verify = False
                    if value:
                        ok_v = await _fill_and_submit_verify(page, vloc, value)
                        session.message = "已提交验证，等待结果…" if ok_v else (
                            "验证提交失败，若页面仍停留请重新扫码"
                        )
                        await page.wait_for_timeout(3000)
                    else:
                        session.message = "验证输入超时，正在刷新二维码，请重新扫码"
                        need_goto = True
                    continue  # 回到循环顶查 cookie

                # 二维码在：保持页面不动，轻轮询等它自然演进
                # （扫码确认 -> 验证表单 / 登录成功跳转）
                if await _extract_qr_base64(page, shot):
                    session.message = "等待扫码（二维码见状态页）"
                    await asyncio.sleep(4)
                    continue

                # 既无验证表单也无二维码：尝试点开登录面板
                await _try_open_login_dialog(page)
                await page.wait_for_timeout(2500)
                if await _extract_qr_base64(page, shot):
                    session.message = "等待扫码（二维码见状态页）"
                    continue
                if await _shot_qr_only(page, shot):
                    session.message = "等待扫码（二维码见状态页）"
                    await asyncio.sleep(4)
                    continue

                # 完全无进展：留档整页并重载
                if time.monotonic() - last_goto > 20:
                    await _shot(
                        page,
                        Path(cfg.debug_dir)
                        / f"douyin_login_page_{int(time.time())}.png",
                    )
                    session.message = "页面无进展，正在重新加载…"
                    need_goto = True
                else:
                    await asyncio.sleep(3)
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


async def _extract_qr_base64(page: Any, shot: Path) -> bool:
    """从 DOM 提取二维码：遍历所有 base64 <img>，取可见且 ≥60px 的最大者。

    2026-11 真实页面核实：登录面板（[id^='login-full-panel']）内二维码为
    base64 PNG（512x512），页面上还有大量小尺寸 base64 图标，必须按尺寸
    筛选取最大，不能取 .first。
    """
    try:
        imgs = page.locator("img[src^='data:image']")
        n = await imgs.count()
    except Exception:
        return False
    best: tuple[float, int] | None = None
    for i in range(n):
        img = imgs.nth(i)
        try:
            if not await img.is_visible():
                continue
            box = await img.bounding_box()
            if box and box["width"] >= 60 and box["height"] >= 60:
                area = box["width"] * box["height"]
                if best is None or area > best[0]:
                    best = (area, i)
        except Exception:
            continue
    if not best:
        return False
    try:
        src = await imgs.nth(best[1]).get_attribute("src") or ""
        data = base64.b64decode(src.split(",", 1)[1])
        if len(data) < 200:
            return False
        shot.write_bytes(data)
        return True
    except Exception:
        return False


async def _detect_verify_input(page: Any) -> tuple[Any, str]:
    """检测扫码后的二次验证表单，返回 (输入框locator, 提示) 或 (None, '')。"""
    for sel in VERIFY_INPUT_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible():
                ph = await loc.get_attribute("placeholder") or sel
                return loc, ph
        except Exception:
            continue
    return None, ""


async def _fill_and_submit_verify(page: Any, loc: Any, value: str) -> bool:
    """把用户提交的验证值填入表单并点确认。"""
    try:
        await loc.fill(value)
        for sel in VERIFY_SUBMIT_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible():
                    await btn.click(timeout=5000)
                    return True
            except Exception:
                continue
        return False
    except Exception:
        log.warning("填写验证表单失败", exc_info=True)
        return False


async def _try_open_login_dialog(page: Any) -> bool:
    """点击页面上的登录入口，打开登录弹窗；成功返回 True。

    面板已打开时点击会被面板拦截而超时，属预期（短超时，不拖慢循环）。
    """
    for sel in LOGIN_BUTTON_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible():
                await loc.click(timeout=5000)
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
