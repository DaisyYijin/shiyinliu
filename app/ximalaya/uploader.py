"""喜马拉雅上传：Playwright 驱动创作中心网页，登录态持久化在 profile 目录。

页面结构可能随喜马拉雅改版变化，因此：
- 每一步都用多候选选择器依次尝试；
- 失败时整页截图存 data/debug/ 便于排查，错误信息带步骤名；
- 上传全局串行（单浏览器上下文），浏览器异常自动重启重试一次。
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from ..config import Config

log = logging.getLogger("ximalaya.uploader")

UPLOAD_PAGE = "https://www.ximalaya.com/reform-upload/page/webCenter/upload"
MANAGE_PAGE = "https://www.ximalaya.com/reform-upload/page/sound/manage/{album_id}"
LOGIN_URL_HINTS = ("passport", "login")


class NotLoggedInError(RuntimeError):
    """尚未登录（登录态失效或从未登录）。"""


def looks_like_login_url(url: str) -> bool:
    """URL 是否被重定向到了登录页。"""
    url = (url or "").lower()
    return any(h in url for h in LOGIN_URL_HINTS)


async def _shot(page: Any, dest: Path) -> None:
    """尽力截图，失败不影响主流程。"""
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(dest), full_page=True)
        log.info("已保存排查截图：%s", dest.name)
    except Exception:
        log.warning("截图失败：%s", dest, exc_info=True)


async def _click_first(page: Any, selectors: list[str], *, timeout: int = 6000) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.click()
            return True
        except Exception:
            continue
    return False


class PlaywrightUploader:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._pw: Any = None
        self._ctx: Any = None
        self._lock = asyncio.Lock()

    # ---------- 浏览器上下文管理 ----------
    async def _ensure_context(self) -> Any:
        if self._ctx is None:
            from playwright.async_api import async_playwright

            self._pw = await async_playwright().start()
            self._ctx = await self._pw.chromium.launch_persistent_context(
                str(Path(self.cfg.profile_dir).resolve()),
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
                viewport={"width": 1440, "height": 900},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
            )
        return self._ctx

    async def _restart(self) -> None:
        for closer in (
            lambda: self._ctx.close(),
            lambda: self._pw.stop(),
        ):
            try:
                if self._ctx or self._pw:
                    await closer()
            except Exception:
                pass
        self._ctx = None
        self._pw = None

    async def close(self) -> None:
        async with self._lock:
            await self._restart()

    # ---------- 登录态 ----------
    async def is_logged_in(self) -> bool:
        ctx = await self._ensure_context()
        page = await ctx.new_page()
        try:
            await page.goto(UPLOAD_PAGE, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)
            return not looks_like_login_url(page.url)
        except Exception:
            log.exception("登录态检测失败")
            return False
        finally:
            await page.close()

    # ---------- 上传 ----------
    async def upload(self, audio_path: str, title: str, author: str) -> str:
        """上传音频到配置的专辑，返回管理页/成功页链接。"""
        async with self._lock:
            last_err: Exception | None = None
            for attempt in (1, 2):
                try:
                    return await self._upload_once(audio_path, title, author)
                except NotLoggedInError:
                    raise
                except Exception as e:  # 浏览器级异常：重启上下文重试一次
                    last_err = e
                    log.warning("上传异常（第 %d 次）：%s", attempt, e)
                    await self._restart()
            raise last_err  # type: ignore[misc]

    async def _upload_once(self, audio_path: str, title: str, author: str) -> str:
        ctx = await self._ensure_context()
        page = await ctx.new_page()
        page.set_default_timeout(20000)
        step = "打开上传页"
        try:
            await page.goto(UPLOAD_PAGE, wait_until="domcontentloaded", timeout=60000)

            step = "登录检测"
            await page.wait_for_timeout(3000)
            if looks_like_login_url(page.url):
                raise NotLoggedInError(
                    "喜马拉雅登录态缺失：请在状态页打开「扫码登录」完成登录后再试"
                )

            step = "选择音频文件"
            file_input = page.locator("input[type=file]").first
            await file_input.wait_for(state="attached", timeout=30000)
            await file_input.set_input_files(str(Path(audio_path).resolve()))

            step = "等待上传完成"
            await self._wait_upload_done(page)

            step = "填写标题"
            await self._fill_title(page, title, author)

            step = "选择专辑"
            await self._select_album(page)

            step = "设置可见范围"
            if self.cfg.ximalaya.visibility == "private":
                await _click_first(page, [
                    "text=私密", "text=仅自己可见", "[aria-label*=私密]",
                ], timeout=3000)

            step = "点击发布"
            ok = await _click_first(page, [
                "button:has-text(\"发布\")", "button:has-text(\"上传\")",
                "button:has-text(\"确定\")", "button:has-text(\"保存\")",
                "text=发布",
            ])
            if not ok:
                raise RuntimeError("未找到发布按钮（页面结构可能已变化，请查看截图）")

            step = "等待发布结果"
            await page.wait_for_timeout(5000)
            for _ in range(24):  # 最多再等 120s 出现成功迹象
                url = page.url
                if "/manage/" in url or "success" in url.lower():
                    break
                content = await page.content()
                if any(k in content for k in ("发布成功", "上传成功", "已发布")):
                    break
                await page.wait_for_timeout(5000)

            album = self.cfg.ximalaya.album_id
            track_url = page.url if "/manage/" in page.url else MANAGE_PAGE.format(album_id=album)
            log.info("发布完成：%s（title=%s）", track_url, title)
            return track_url
        except NotLoggedInError:
            raise
        except Exception as e:
            await _shot(page, Path(self.cfg.debug_dir) / f"xm_{step}_{int(time.time())}.png")
            raise RuntimeError(f"上传失败于「{step}」：{e}") from e
        finally:
            try:
                await page.close()
            except Exception:
                pass

    # ---------- 步骤辅助 ----------
    async def _wait_upload_done(self, page: Any) -> None:
        """等待文件上传/转码完成：进度文案或标题输入框可用。"""
        deadline = time.monotonic() + self.cfg.ximalaya.upload_timeout_seconds
        while time.monotonic() < deadline:
            content = await page.content()
            if any(k in content for k in ("上传成功", "转码完成", "上传完成")):
                return
            # 标题输入框出现通常意味着可以编辑了
            for sel in ("input[placeholder*='标题']", "input[placeholder*='名称']"):
                loc = page.locator(sel).first
                try:
                    if await loc.is_visible():
                        return
                except Exception:
                    continue
            await page.wait_for_timeout(3000)
        raise TimeoutError("等待上传完成超时（请检查网络或加大 upload_timeout_seconds）")

    async def _fill_title(self, page: Any, title: str, author: str) -> None:
        filled = False
        for sel in ("input[placeholder*='标题']", "input[placeholder*='名称']",
                    ".title input", "input[maxlength]"):
            loc = page.locator(sel).first
            try:
                if await loc.is_visible():
                    await loc.fill(title[:60])
                    filled = True
                    break
            except Exception:
                continue
        if not filled:
            log.warning("未找到标题输入框，沿用文件名")
        # 简介可选：作者写入简介（失败忽略）
        for sel in ("textarea", "div[contenteditable='true']"):
            loc = page.locator(sel).first
            try:
                if await loc.is_visible():
                    text = author and f"原视频作者：{author}"
                    if text:
                        await loc.fill(text[:200])
                    break
            except Exception:
                continue

    async def _select_album(self, page: Any) -> None:
        album_id = self.cfg.ximalaya.album_id
        if not album_id:
            log.warning("未配置 album_id，将使用默认专辑")
            return
        # 优先原生 select（value 或 label 匹配）
        selects = page.locator("select")
        try:
            n = await selects.count()
        except Exception:
            n = 0
        for i in range(n):
            sel_el = selects.nth(i)
            try:
                opts = sel_el.locator("option")
                m = await opts.count()
                for j in range(m):
                    opt = opts.nth(j)
                    val = await opt.get_attribute("value")
                    label = (await opt.inner_text()).strip()
                    if val == album_id or label == album_id:
                        if val == album_id:
                            await sel_el.select_option(value=val)
                        else:
                            await sel_el.select_option(label=label)
                        return
            except Exception:
                continue
        # 自定义下拉：点开容器后按文本/属性匹配 album_id
        if await _click_first(page, [
            ".ant-select-selector", "[class*='album'] [class*='select']",
            "[class*='select']", "text=选择专辑",
        ], timeout=4000):
            if await _click_first(page, [
                f"[title='{album_id}']", f"option:has-text('{album_id}')",
                f"li:has-text('{album_id}')",
                f"[class*='option']:has-text('{album_id}')",
            ], timeout=6000):
                return
        # 静默失败风险：专辑不对会上传到默认专辑，记录日志供用户核对
        log.warning("未能显式选择专辑 %s（页面结构可能变化，可能落入默认专辑）", album_id)
