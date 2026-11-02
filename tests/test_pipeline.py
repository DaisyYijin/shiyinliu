"""流水线测试：fake 四件套走完整状态机（下载→转码→上传、失败重试、断点续推）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Config, DouyinAccount
from app.db import Store
from app.pipeline import Pipeline

SEC_UID = "MS4wLjABAAAAabcdefghij0123456789_-abc"
AW_ID = "7412345678901234567"


def make_cfg(tmp_path: Path) -> Config:
    cfg = Config()
    cfg.data_dir = tmp_path / "data"
    cfg.media_dir = tmp_path / "media"
    (cfg.data_dir / "tmp").mkdir(parents=True, exist_ok=True)
    return cfg


class FakeMonitor:
    def __init__(self, posts=None, detail=None):
        self.posts = posts or []
        self.detail = detail

    async def list_posts(self, sec_uid, limit, fetch_all=False):
        return self.posts if fetch_all else self.posts[:limit]

    async def get_post(self, aweme_id):
        return self.detail


class FakeDownloader:
    def __init__(self):
        self.calls = []

    async def download(self, play_url, dest_dir, aweme_id):
        self.calls.append((play_url, aweme_id))
        p = Path(dest_dir) / f"{aweme_id}.mp4"
        p.write_bytes(b"FAKE_VIDEO" * 1000)
        return str(p)


class FakeConverter:
    def __init__(self):
        self.calls = []

    async def convert(self, video_path, base_name):
        self.calls.append(video_path)
        out = Path(video_path).with_suffix(".fake_audio")
        out.write_bytes(b"FAKE_AUDIO")
        return str(out)


class FakeUploader:
    def __init__(self, fail_first=0):
        self.calls = []
        self.fail_first = fail_first

    async def upload(self, audio_path, title, author):
        self.calls.append((audio_path, title, author))
        if self.fail_first > 0:
            self.fail_first -= 1
            raise RuntimeError("上传失败（模拟）")
        return "https://www.ximalaya.com/sound/999999"


def make_pipeline(tmp_path, posts=None, detail=None, fail_first=0, keep_video=False):
    cfg = make_cfg(tmp_path)
    cfg.keep_video = keep_video
    store = Store(cfg.db_path)
    store.set_max_attempts(cfg.max_attempts)
    store.sync_config_accounts([DouyinAccount(name="测试号", sec_uid=SEC_UID)])
    dl, cv, up = FakeDownloader(), FakeConverter(), FakeUploader(fail_first)
    pipe = Pipeline(cfg, store, FakeMonitor(posts, detail), dl, cv, up)
    return pipe, store, dl, cv, up


@pytest.mark.asyncio
async def test_poll_once_full_flow(tmp_path):
    posts = [{"aweme_id": AW_ID, "title": "新视频", "author": "作者",
              "publish_time": 1730000000, "meta": {"play_url": "http://p/1"}}]
    pipe, store, dl, cv, up = make_pipeline(tmp_path, posts=posts)
    result = await pipe.poll_once()
    assert result["discovered"] == 1 and result["uploaded"] == 1
    assert result["accounts"][0].get("first_full_fetch") is True  # 首轮全量标记
    rec = store.get(AW_ID)
    assert rec["status"] == "uploaded"
    assert rec["xima_track"] == "https://www.ximalaya.com/sound/999999"
    assert dl.calls and cv.calls and up.calls
    # keep_video=False：临时视频应被清理
    assert not Path(rec["video_path"]).exists()
    # 再次轮询：去重，不重复处理
    result2 = await pipe.poll_once()
    assert result2["discovered"] == 0 and result2["processed"] == 0


@pytest.mark.asyncio
async def test_fail_then_retry(tmp_path):
    posts = [{"aweme_id": AW_ID, "title": "t", "author": "a",
              "publish_time": 1, "meta": {"play_url": "http://p/1"}}]
    pipe, store, dl, cv, up = make_pipeline(tmp_path, posts=posts, fail_first=1)
    r1 = await pipe.poll_once()
    assert r1["failed"] == 1
    rec = store.get(AW_ID)
    assert rec["status"] == "failed" and rec["attempts"] == 1 and "模拟" in rec["error"]
    # 下一轮重试成功
    r2 = await pipe.process_pending()
    assert r2["uploaded"] == 1
    assert store.get(AW_ID)["status"] == "uploaded"


@pytest.mark.asyncio
async def test_resume_from_downloaded(tmp_path):
    pipe, store, dl, cv, up = make_pipeline(tmp_path)
    v = tmp_path / "already.mp4"
    v.write_bytes(b"V")
    store.upsert_discovered({"aweme_id": AW_ID, "account": "测试号", "title": "断点",
                             "publish_time": 1, "meta": {"play_url": "http://p"}})
    store.update(AW_ID, status="downloaded", video_path=str(v))
    await pipe.process_pending()
    assert not dl.calls                      # 不重新下载
    assert cv.calls == [str(v)]              # 直接从转码开始
    assert store.get(AW_ID)["status"] == "uploaded"


@pytest.mark.asyncio
async def test_submit_video_url(tmp_path, monkeypatch):
    detail = {"aweme_id": AW_ID, "title": "手动提交", "author": "x",
              "publish_time": 2, "meta": {"play_url": "http://p/9"}}
    pipe, store, *_ = make_pipeline(tmp_path, detail=detail)
    async def fake_expand(url):
        return f"https://www.douyin.com/video/{AW_ID}"
    monkeypatch.setattr("app.douyin.monitor.expand_share_link", fake_expand)
    res = await pipe.submit_url("https://v.douyin.com/whatever/")
    assert res["new"] is True and res["status"] == "uploaded"
    rec = store.get(AW_ID)
    assert rec["account"] == "手动提交"
