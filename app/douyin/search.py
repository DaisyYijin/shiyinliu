"""博主搜索：登录态打开抖音搜索页（用户类型），提取候选博主昵称与 sec_uid。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..config import SEC_UID_RE, Config
from .login import browser_lock

log = logging.getLogger("douyin.search")

SEARCH_URL = "https://www.douyin.com/search/{kw}?type=user"


async def search_creators(cfg: Config, keyword: str, limit: int = 10) -> list[dict]:
    """返回 [{nickname, sec_uid}]，按页面出现顺序去重（需先完成抖音扫码登录）。"""
    from playwright.async_api import async_playwright

    keyword = (keyword or "").strip()
    if not keyword:
        return []
    pw = await async_playwright().start()
    ctx = None
    out: list[dict] = []
    try:
        async with browser_lock:
            ctx = await pw.chromium.launch_persistent_context(
                str((Path(cfg.data_dir) / "douyin_profile").resolve()),
                headless=True,
                viewport={"width": 1280, "height": 900},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
            )
            page = await ctx.new_page()
            await page.goto(
                SEARCH_URL.format(kw=quote(keyword)),
                wait_until="domcontentloaded",
                timeout=60000,
            )
            await page.wait_for_timeout(4500)
            links = page.locator("a[href*='/user/MS4wLjAB']")
            n = await links.count()
            seen: set[str] = set()
            for i in range(min(n, 40)):
                a = links.nth(i)
                try:
                    href = await a.get_attribute("href") or ""
                    m = SEC_UID_RE.search(href)
                    if not m:
                        continue
                    sec_uid = m.group(0)
                    if sec_uid in seen:
                        continue
                    text = (await a.inner_text()).strip()
                    nickname = text.split("\n")[0].strip() if text else ""
                    if not nickname or nickname.startswith("MS4wLjAB"):
                        nickname = f"博主{i + 1}"
                    seen.add(sec_uid)
                    out.append({"nickname": nickname[:40], "sec_uid": sec_uid})
                    if len(out) >= limit:
                        break
                except Exception:
                    continue
    finally:
        try:
            if ctx:
                await ctx.close()
            await pw.stop()
        except Exception:
            pass
    log.info("搜索「%s」：%d 个候选", keyword, len(out))
    return out
