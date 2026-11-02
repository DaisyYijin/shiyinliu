"""转换器测试：真实 ffmpeg 转码（无 ffmpeg 环境自动跳过，CI 会装 ffmpeg 全量跑）。"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.audio.converter import FFmpegConverter
from app.config import Config

HAVE_FFMPEG = shutil.which("ffmpeg") is not None or bool(os.environ.get("FFMPEG_BIN"))
pytestmark = pytest.mark.skipif(not HAVE_FFMPEG, reason="环境无 ffmpeg")


def make_video(path: Path, with_audio: bool = True) -> None:
    cmd = [os.environ.get("FFMPEG_BIN", "ffmpeg"), "-y"]
    if with_audio:
        cmd += [
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=10",
            "-shortest", "-c:a", "aac", "-c:v", "mpeg4",
        ]
    else:
        cmd += [
            "-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=10",
            "-an", "-c:v", "mpeg4",
        ]
    cmd += [str(path)]
    subprocess.run(cmd, check=True, capture_output=True)


async def test_convert_mp3(tmp_path):
    video = tmp_path / "v.mp4"
    make_video(video)
    cfg = Config()
    cfg.media_dir = tmp_path / "media"
    out = await FFmpegConverter(cfg).convert(str(video), "测试标题_123456")
    p = Path(out)
    assert p.exists() and p.name == "测试标题_123456.mp3"
    assert p.stat().st_size > 1000


async def test_convert_m4a(tmp_path):
    video = tmp_path / "v.mp4"
    make_video(video)
    cfg = Config()
    cfg.media_dir = tmp_path / "media"
    cfg.audio.format = "m4a"
    out = await FFmpegConverter(cfg).convert(str(video), "m4a测试")
    assert Path(out).suffix == ".m4a" and Path(out).stat().st_size > 1000


async def test_convert_no_audio_track(tmp_path):
    video = tmp_path / "silent.mp4"
    make_video(video, with_audio=False)
    cfg = Config()
    cfg.media_dir = tmp_path / "media"
    with pytest.raises(RuntimeError, match="音轨"):
        await FFmpegConverter(cfg).convert(str(video), "无声视频")
