"""轮询调度：自写 asyncio 循环，间隔可配，异常不打断。"""
from __future__ import annotations

import asyncio
import logging

from .pipeline import Pipeline

log = logging.getLogger("scheduler")


class PollScheduler:
    def __init__(self, pipeline: Pipeline, interval_minutes: int, poll_on_start: bool = True):
        self.pipeline = pipeline
        self.interval = max(1, int(interval_minutes)) * 60
        self.poll_on_start = poll_on_start
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._stopped = False

    def start(self) -> None:
        self._task = asyncio.get_event_loop().create_task(self._run(), name="poller")

    async def stop(self) -> None:
        self._stopped = True
        self._wake.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def poll_now(self) -> None:
        """手动唤醒（等待下一次循环立即执行）。"""
        self._wake.set()

    async def _run(self) -> None:
        first = True
        while not self._stopped:
            if first and not self.poll_on_start:
                first = False
            else:
                try:
                    await self.pipeline.poll_once()
                except Exception:
                    log.exception("轮询异常（忽略并继续）")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
