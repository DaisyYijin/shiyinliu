"""FastAPI 入口：组装配置、存储、流水线与调度器。"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .audio.converter import FFmpegConverter
from .config import Config, load_config
from .db import Store
from .douyin.downloader import HttpDownloader
from .douyin.monitor import F2Monitor
from .pipeline import Pipeline
from .scheduler import PollScheduler
from .web.routes import router
from .ximalaya.uploader import PlaywrightUploader

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("main")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@dataclass
class AppContext:
    cfg: Config
    store: Store
    pipeline: Pipeline
    scheduler: PollScheduler
    login_session: Any = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config()
    for d in (cfg.data_dir, cfg.tmp_dir, cfg.debug_dir, cfg.media_dir):
        Path(d).mkdir(parents=True, exist_ok=True)

    store = Store(cfg.db_path)
    store.set_max_attempts(cfg.max_attempts)

    pipeline = Pipeline(
        cfg=cfg,
        store=store,
        monitor=F2Monitor(cfg),
        downloader=HttpDownloader(cfg),
        converter=FFmpegConverter(cfg),
        uploader=PlaywrightUploader(cfg),
    )
    scheduler = PollScheduler(
        pipeline, cfg.poll_interval_minutes, poll_on_start=cfg.poll_on_start
    )
    scheduler.start()
    app.state.ctx = AppContext(cfg=cfg, store=store, pipeline=pipeline, scheduler=scheduler)
    log.info("拾音流已启动：账号 %d 个，轮询间隔 %d 分钟",
             len(cfg.douyin.accounts), cfg.poll_interval_minutes)
    try:
        yield
    finally:
        await scheduler.stop()
        try:
            await pipeline.uploader.close()  # 关闭 Playwright 浏览器上下文
        except Exception:
            pass
        store.close()


app = FastAPI(title="拾音流", version="0.1.0", lifespan=lifespan)
app.include_router(router)
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
