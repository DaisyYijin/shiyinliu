"""处理流水线：发现新视频 -> 下载 -> 转音频 -> 上传喜马拉雅，全程状态落库。"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Protocol

from .config import Config, extract_sec_uid
from .db import Store

log = logging.getLogger("pipeline")


class Monitor(Protocol):
    async def list_posts(self, sec_uid: str, limit: int) -> list[dict[str, Any]]: ...


class Downloader(Protocol):
    async def download(self, play_url: str, dest_dir: Any, aweme_id: str) -> str: ...


class Converter(Protocol):
    async def convert(self, video_path: str, base_name: str) -> str: ...


class Uploader(Protocol):
    async def upload(self, audio_path: str, title: str, author: str) -> str: ...


AWEME_ID_RE = re.compile(r"/(?:video|note)/(\d{6,})")

STATUS_DISCOVERED = "discovered"
STATUS_DOWNLOADED = "downloaded"
STATUS_CONVERTED = "converted"
STATUS_UPLOADED = "uploaded"
STATUS_FAILED = "failed"


class Pipeline:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        monitor: Monitor,
        downloader: Downloader,
        converter: Converter,
        uploader: Uploader,
    ):
        self.cfg = cfg
        self.store = store
        self.monitor = monitor
        self.downloader = downloader
        self.converter = converter
        self.uploader = uploader
        self._lock = asyncio.Lock()
        self._last_poll: dict[str, Any] = {"at": 0, "result": None, "running": False}

    # ---- 对外 ------------------------------------------------------
    @property
    def last_poll(self) -> dict[str, Any]:
        return self._last_poll

    async def poll_once(self) -> dict[str, Any]:
        """拉取所有账号最新作品并处理增量，带全局锁防止并发轮询。"""
        if self._lock.locked():
            return {"skipped": "已有轮询进行中"}
        async with self._lock:
            self._last_poll["running"] = True
            t0 = asyncio.get_event_loop().time()
            result: dict[str, Any] = {
                "discovered": 0,
                "processed": 0,
                "uploaded": 0,
                "failed": 0,
                "accounts": [],
            }
            try:
                # 1) 发现新作品（监控账号来自配置文件与状态页搜索添加，统一入 SQLite）
                for acct in self.store.list_accounts():
                    sec_uid, name = acct["sec_uid"], acct["name"]
                    first_full = not acct["fetched_all"]
                    try:
                        posts = await self.monitor.list_posts(
                            sec_uid,
                            self.cfg.douyin.max_posts_per_poll,
                            fetch_all=first_full,
                        )
                        n_new = 0
                        for p in posts:
                            if self.store.upsert_discovered({**p, "account": name}):
                                n_new += 1
                        if first_full:
                            self.store.set_fetched_all(sec_uid)
                        result["discovered"] += n_new
                        entry: dict[str, Any] = {
                            "name": name, "posts": len(posts), "new": n_new,
                        }
                        if first_full:
                            entry["first_full_fetch"] = True
                        result["accounts"].append(entry)
                    except Exception as e:  # 单账号失败不影响其他账号
                        log.exception("账号 %s 拉取失败", name)
                        result["accounts"].append(
                            {"name": name, "error": str(e)[:300]}
                        )
                # 2) 处理积压
                proc = await self.process_pending()
                result.update(proc)
            finally:
                result["seconds"] = round(asyncio.get_event_loop().time() - t0, 1)
                self._last_poll = {"at": int(asyncio.get_event_loop().time()),
                                   "result": result, "running": False}
            return result

    async def process_pending(self) -> dict[str, Any]:
        stats = {"processed": 0, "uploaded": 0, "failed": 0}
        for rec in self.store.pending(
            (STATUS_DISCOVERED, STATUS_DOWNLOADED, STATUS_CONVERTED)
        ):
            ok, stage = await self._advance(rec)
            stats["processed"] += 1
            if ok and stage == STATUS_UPLOADED:
                stats["uploaded"] += 1
            elif not ok:
                stats["failed"] += 1
        return stats

    async def submit_url(self, url: str) -> dict[str, Any]:
        """手动提交：支持视频链接（含短链）与博主主页链接，走同一流水线。"""
        from .douyin.monitor import expand_share_link

        url = (url or "").strip()
        if not url:
            raise ValueError("链接为空")
        real = await expand_share_link(url)
        m = AWEME_ID_RE.search(real)
        if m:
            aweme_id = m.group(1)
            post = await self.monitor.get_post(aweme_id)
            is_new = self.store.upsert_discovered({**post, "account": "手动提交"})
            await self.process_pending()
            rec = self.store.get(aweme_id)
            return {"aweme_id": aweme_id, "new": is_new, "status": rec["status"] if rec else None}
        sec_uid = extract_sec_uid(real) or extract_sec_uid(url)
        if sec_uid:
            posts = await self.monitor.list_posts(
                sec_uid, self.cfg.douyin.max_posts_per_poll
            )
            if not posts:
                raise RuntimeError("未拉取到该主页的作品")
            for p in posts:
                self.store.upsert_discovered({**p, "account": "手动提交"})
            await self.process_pending()
            return {"submitted": len(posts), "sec_uid": sec_uid}
        raise ValueError("无法识别的链接：请粘贴抖音视频链接或博主主页链接")

    # ---- 内部：单条记录推进 ----------------------------------------
    async def _advance(self, rec: dict[str, Any]) -> tuple[bool, str]:
        """按已有产物决定起点：有音频 -> 上传；有视频 -> 转码；否则下载。"""
        aweme_id = rec["aweme_id"]
        try:
            if rec["audio_path"]:
                await self._do_upload(rec)
                return True, STATUS_UPLOADED
            if rec["video_path"]:
                await self._do_convert(rec)
                return True, STATUS_UPLOADED
            await self._do_download(rec)
            return True, STATUS_UPLOADED
        except Exception as e:
            log.exception("处理 %s 失败", aweme_id)
            self.store.update(aweme_id, status=STATUS_FAILED, error=str(e)[:1500])
            return False, STATUS_FAILED

    async def _do_download(self, rec: dict[str, Any]) -> None:
        aweme_id = rec["aweme_id"]
        self.store.update(aweme_id, status="downloading",
                          attempts=rec["attempts"] + 1, error="")
        meta = rec.get("meta") or {}
        play_url = meta.get("play_url") or ""
        if not play_url:
            raise RuntimeError("缺少播放地址（meta.play_url 为空）")
        video_path = await self.downloader.download(
            play_url, self.cfg.tmp_dir, aweme_id
        )
        self.store.update(aweme_id, status=STATUS_DOWNLOADED, video_path=video_path)
        await self._do_convert({**rec, "video_path": video_path})

    async def _do_convert(self, rec: dict[str, Any]) -> None:
        aweme_id = rec["aweme_id"]
        self.store.update(aweme_id, status="converting", error="")
        # 标题 + aweme_id 尾号，避免同名互相覆盖
        base = _safe_name(rec.get("title") or aweme_id)[:60] + "_" + aweme_id[-6:]
        audio_path = await self.converter.convert(rec["video_path"], base)
        self.store.update(aweme_id, status=STATUS_CONVERTED, audio_path=audio_path)
        await self._do_upload({**rec, "audio_path": audio_path})

    async def _do_upload(self, rec: dict[str, Any]) -> None:
        aweme_id = rec["aweme_id"]
        self.store.update(aweme_id, status="uploading", error="")
        track = await self.uploader.upload(
            rec["audio_path"], rec.get("title") or aweme_id, rec.get("author") or ""
        )
        self.store.update(aweme_id, status=STATUS_UPLOADED, xima_track=track)
        if not self.cfg.keep_video:
            _remove_quiet(rec.get("video_path"))
        log.info("完成：%s -> %s", aweme_id, track)


def _safe_name(name: str, maxlen: int = 80) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', " ", name).strip()
    return (name[:maxlen] or "untitled").strip()


def _remove_quiet(path: str | None) -> None:
    if not path:
        return
    try:
        from pathlib import Path

        Path(path).unlink(missing_ok=True)
    except Exception:
        log.debug("清理失败：%s", path, exc_info=True)
