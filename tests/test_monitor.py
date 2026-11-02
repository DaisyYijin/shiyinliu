"""抖音监控模块测试：不依赖真实 f2（stub 掉 DouyinHandler）。"""
from __future__ import annotations

import pytest

from app.config import Config
from app.douyin.monitor import F2Monitor, extract_aweme

PLAY_URL = "https://aweme.snssdk.com/aweme/v1/play/?video_id=v0d00fg10000&ratio=1080p&line=0"


def sample_aweme(aid="7412345678901234567", desc="一个标题", with_bitrate=True, **over):
    video = {"duration": 61200}
    if with_bitrate:
        video["bit_rate"] = [{"play_addr": {"url_list": [PLAY_URL]}}]
    else:
        video["play_addr"] = {"url_list": ["https://fallback/play.mp4"]}
    aw = {
        "aweme_id": aid,
        "desc": desc,
        "create_time": 1730000000,
        "author": {"nickname": "某博主"},
        "video": video,
    }
    aw.update(over)
    return aw


class FakeFilter:
    def __init__(self, raw):
        self._raw = raw

    def _to_raw(self):
        return self._raw


class FakeHandler:
    """模拟 f2 DouyinHandler：只实现监控用到的两个方法。"""

    def __init__(self, pages=None, detail=None):
        self.pages = pages or []
        self.detail = detail
        self.calls = []

    async def fetch_user_post_videos(self, sec_uid, min_cursor=0, max_cursor=0,
                                     page_counts=20, max_counts=None):
        self.calls.append(("list", sec_uid, page_counts))
        for p in self.pages:
            yield FakeFilter(p)

    async def fetch_one_video(self, aweme_id):
        self.calls.append(("detail", aweme_id))
        return FakeFilter(self.detail)


def make_monitor(pages=None, detail=None) -> tuple[F2Monitor, FakeHandler]:
    m = F2Monitor(Config())
    h = FakeHandler(pages, detail)
    m._client = h
    return m, h


# ---------- extract_aweme ----------

def test_extract_aweme_bit_rate():
    item = extract_aweme(sample_aweme())
    assert item["aweme_id"] == "7412345678901234567"
    assert item["title"] == "一个标题"
    assert item["author"] == "某博主"
    assert item["publish_time"] == 1730000000
    assert item["meta"]["play_url"] == PLAY_URL


def test_extract_aweme_fallback_play_addr():
    item = extract_aweme(sample_aweme(with_bitrate=False))
    assert item["meta"]["play_url"] == "https://fallback/play.mp4"


def test_extract_aweme_skips_image_post():
    assert extract_aweme(sample_aweme(with_bitrate=False, video={"duration": 1})) is None


def test_extract_aweme_title_fallback_and_str_time():
    aw = sample_aweme(desc="", create_time="1730000001")
    item = extract_aweme(aw)
    assert item["title"] == aw["aweme_id"]
    assert item["publish_time"] == 1730000001


# ---------- list_posts / get_post ----------

@pytest.mark.asyncio
async def test_list_posts_first_page_sorted():
    pages = [{"aweme_list": [
        sample_aweme(aid="2", create_time=200),
        sample_aweme(aid="1", create_time=300),
        sample_aweme(aid="3", create_time=100),
    ]}]
    m, h = make_monitor(pages=pages)
    posts = await m.list_posts("sec_uid_x", 2)
    assert [p["aweme_id"] for p in posts] == ["1", "2"]  # 按发布时间倒序取前 N
    assert h.calls[0][1] == "sec_uid_x"


@pytest.mark.asyncio
async def test_list_posts_fetch_all_pages():
    pages = [
        {"aweme_list": [sample_aweme(aid=str(i)) for i in range(3)]},
        {"aweme_list": [sample_aweme(aid=str(i)) for i in range(3, 5)]},
    ]
    m, _ = make_monitor(pages=pages)
    posts = await m.list_posts("sec_uid_x", 2, fetch_all=True)
    assert len(posts) == 5  # 全量翻页，不受 limit 限制
    # 非全量仍只取第一页
    m2, _ = make_monitor(pages=pages)
    assert len(await m2.list_posts("sec_uid_x", 2)) == 2


@pytest.mark.asyncio
async def test_get_post_detail():
    m, h = make_monitor(detail={"aweme_detail": sample_aweme()})
    item = await m.get_post("7412345678901234567")
    assert item["meta"]["play_url"] == PLAY_URL


@pytest.mark.asyncio
async def test_get_post_invalid_raises():
    m, _ = make_monitor(detail={"aweme_detail": {"foo": 1}})
    with pytest.raises(RuntimeError):
        await m.get_post("123")
