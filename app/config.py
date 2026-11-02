"""配置加载：config/app.yaml 为主，环境变量覆盖关键路径与端口。"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_DOUYIN_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

SEC_UID_RE = re.compile(r"MS4wLjABAAAA[A-Za-z0-9_\-]{20,}")


@dataclass
class DouyinAccount:
    name: str
    sec_uid: str


@dataclass
class DouyinConfig:
    accounts: list[DouyinAccount] = field(default_factory=list)
    cookie: str = ""
    max_posts_per_poll: int = 20


@dataclass
class AudioConfig:
    format: str = "mp3"
    bitrate: str = "192k"


@dataclass
class XimalayaConfig:
    album_id: str = ""
    visibility: str = "public"  # public | private
    upload_timeout_seconds: int = 600


@dataclass
class Config:
    port: int = 8300
    poll_interval_minutes: int = 30
    poll_on_start: bool = True
    max_attempts: int = 3
    keep_video: bool = False
    douyin: DouyinConfig = field(default_factory=DouyinConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    ximalaya: XimalayaConfig = field(default_factory=XimalayaConfig)
    data_dir: Path = Path("data")
    media_dir: Path = Path("media")

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def debug_dir(self) -> Path:
        return self.data_dir / "debug"

    @property
    def profile_dir(self) -> Path:
        return self.data_dir / "ximalaya_profile"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "records.db"


def extract_sec_uid(text: str) -> str | None:
    """从主页链接 / 分享文本 / 裸 sec_uid 中提取 sec_uid。"""
    if not text:
        return None
    text = text.strip()
    m = SEC_UID_RE.search(text)
    return m.group(0) if m else None


def _parse_accounts(raw: Any) -> list[DouyinAccount]:
    accounts: list[DouyinAccount] = []
    for i, item in enumerate(raw or []):
        if isinstance(item, str):
            name, target = f"账号{i + 1}", item
        else:
            name = str(item.get("name") or f"账号{i + 1}").strip()
            target = str(item.get("url") or item.get("sec_uid") or item.get("id") or "")
        sec_uid = extract_sec_uid(target)
        if not sec_uid:
            raise ValueError(
                f"配置错误：账号「{name}」无法从其配置中解析出 sec_uid（{target!r}）。"
                "请填写 https://www.douyin.com/user/<sec_uid> 形式的主页链接。"
            )
        accounts.append(DouyinAccount(name=name, sec_uid=sec_uid))
    return accounts


def load_config(path: Path | str | None = None) -> Config:
    cfg_path = Path(path or os.environ.get("SY_CONFIG", "config/app.yaml"))
    raw: dict[str, Any] = {}
    if cfg_path.exists():
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    cfg = Config(
        port=int(os.environ.get("PORT", raw.get("port", 8300))),
        poll_interval_minutes=int(raw.get("poll_interval_minutes", 30)),
        poll_on_start=bool(raw.get("poll_on_start", True)),
        max_attempts=int(raw.get("max_attempts", 3)),
        keep_video=bool(raw.get("keep_video", False)),
        douyin=DouyinConfig(
            accounts=_parse_accounts(raw.get("douyin", {}).get("accounts")),
            cookie=str(raw.get("douyin", {}).get("cookie", "") or ""),
            max_posts_per_poll=int(raw.get("douyin", {}).get("max_posts_per_poll", 20)),
        ),
        audio=AudioConfig(
            format=str(raw.get("audio", {}).get("format", "mp3")).lower(),
            bitrate=str(raw.get("audio", {}).get("bitrate", "192k")),
        ),
        ximalaya=XimalayaConfig(
            album_id=str(raw.get("ximalaya", {}).get("album_id", "") or ""),
            visibility=str(raw.get("ximalaya", {}).get("visibility", "public")),
            upload_timeout_seconds=int(
                raw.get("ximalaya", {}).get("upload_timeout_seconds", 600)
            ),
        ),
        data_dir=Path(os.environ.get("SY_DATA_DIR", raw.get("data_dir", "data"))),
        media_dir=Path(os.environ.get("SY_MEDIA_DIR", raw.get("media_dir", "media"))),
    )

    if cfg.audio.format not in ("mp3", "m4a", "aac"):
        raise ValueError(f"不支持的音频格式：{cfg.audio.format}（可选 mp3/m4a/aac）")
    return cfg
