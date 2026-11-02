"""音频提取：ffmpeg 从视频提取音轨并转 mp3/m4a/aac。"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from ..config import Config

log = logging.getLogger("audio.converter")

FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")


class FFmpegConverter:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _output_path(self, base_name: str) -> Path:
        Path(self.cfg.media_dir).mkdir(parents=True, exist_ok=True)
        safe = (base_name or "").strip() or "untitled"
        return Path(self.cfg.media_dir) / f"{safe}.{self.cfg.audio.format}"

    async def convert(self, video_path: str, base_name: str) -> str:
        """提取音轨转目标格式，返回输出文件路径。失败抛 RuntimeError（含原因）。"""
        fmt = self.cfg.audio.format
        out = self._output_path(base_name)
        if fmt == "mp3":
            codec_args = ["-c:a", "libmp3lame", "-b:a", self.cfg.audio.bitrate]
        else:  # m4a / aac
            codec_args = ["-c:a", "aac", "-b:a", self.cfg.audio.bitrate]
        cmd = [
            FFMPEG_BIN, "-y", "-i", video_path, "-vn", *codec_args, str(out),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise RuntimeError("未找到 ffmpeg 可执行文件，请确认镜像/系统已安装 ffmpeg")

        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode("utf-8", "ignore")
            if "does not contain any stream" in err:
                raise RuntimeError("视频没有可用音轨，无法提取音频")
            raise RuntimeError(
                f"ffmpeg 转换失败（exit={proc.returncode}）：{err[-600:]}"
            )
        if not out.exists() or out.stat().st_size < 1024:
            raise RuntimeError("ffmpeg 输出异常：音频文件过小或为空")
        log.info("已转音频 %s（%.1f MB）", out.name, out.stat().st_size / 1048576)
        return str(out)
