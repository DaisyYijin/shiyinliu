"""抖音作品监控：基于 f2 库拉取博主作品列表（a_bogus 签名由库内自动处理）。

数据字段统一从 f2 filter 的 _to_raw() 原始 JSON 中提取，避免依赖 f2 内部
filter 属性命名（jsonpath 属性随版本变动，而抖音原始结构更稳定）。
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from ..config import DEFAULT_DOUYIN_UA, Config

log = logging.getLogger("douyin.monitor")


def extract_aweme(aw: dict[str, Any]) -> dict[str, Any] | None:
    """从抖音 aweme 原始 JSON 提取统一摘要；无效条目 / 图集（无视频）返回 None。"""
    aweme_id = str(aw.get("aweme_id") or "")
    if not aweme_id:
        return None
    video = aw.get("video") or {}
    urls: list[str] = []
    bit_rate = video.get("bit_rate") or []
    if bit_rate:
        urls = (bit_rate[0].get("play_addr") or {}).get("url_list") or []
    if not urls:
        urls = (video.get("play_addr") or {}).get("url_list") or []
    if not urls:  # 图集等无视频内容，无法转音频
        return None
    return {
        "aweme_id": aweme_id,
        "title": (aw.get("desc") or "").strip() or aweme_id,
        "author": ((aw.get("author") or {}).get("nickname") or "").strip(),
        "publish_time": int(aw.get("create_time") or 0),
        "meta": {
            "play_url": urls[0],
            "duration_ms": int(video.get("duration") or 0),
        },
    }


class F2Monitor:
    """封装 f2 的 DouyinHandler，输出统一的作品摘要 dict 列表。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._client: Any = None

    def _handler(self) -> Any:
        if self._client is None:
            from f2.apps.douyin.handler import DouyinHandler

            kwargs = {
                "headers": {
                    "User-Agent": DEFAULT_DOUYIN_UA,
                    "Referer": "https://www.douyin.com/",
                },
                "proxies": {"http://": None, "https://": None},
                "timeout": 15,
                "cookie": self.cfg.douyin.cookie or "",
            }
            self._client = DouyinHandler(kwargs)
        return self._client

    async def list_posts(self, sec_uid: str, limit: int) -> list[dict[str, Any]]:
        """拉取博主第一页作品（监控场景第一页即可覆盖增量）。"""
        handler = self._handler()
        posts: list[dict[str, Any]] = []
        page_counts = max(1, min(int(limit), 50))
        async for page in handler.fetch_user_post_videos(sec_uid, 0, 0, page_counts):
            raw = page._to_raw() or {}
            for aw in raw.get("aweme_list") or []:
                item = extract_aweme(aw)
                if item:
                    posts.append(item)
            break  # 只取第一页
        posts.sort(key=lambda p: p["publish_time"], reverse=True)
        log.info("拉取 %s 作品：%d 条（取 %d）", sec_uid[:16], len(posts), min(len(posts), limit))
        return posts[:limit]

    async def get_post(self, aweme_id: str) -> dict[str, Any]:
        """获取单个视频详情（手动提交入口用）。"""
        handler = self._handler()
        res = handler.fetch_one_video(aweme_id)
        # 兼容返回 coroutine / async generator 两种形态
        if hasattr(res, "__aiter__"):
            flt = await res.__anext__()
        else:
            flt = await res
        raw = flt._to_raw() or {}
        aw = raw.get("aweme_detail") or raw
        item = extract_aweme(aw)
        if not item:
            raise RuntimeError(f"未取到视频 {aweme_id} 的有效数据（可能已删除或接口失败）")
        return item


async def expand_share_link(url: str) -> str:
    """展开 v.douyin.com 分享短链为最终链接；非短链原样返回。"""
    if "v.douyin.com" not in url and "/share/" not in url:
        return url
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=15,
        headers={"User-Agent": DEFAULT_DOUYIN_UA},
    ) as client:
        resp = await client.get(url)
        return str(resp.url)
