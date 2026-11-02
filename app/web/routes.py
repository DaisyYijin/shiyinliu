"""HTTP API：状态查询、手动触发、手动提交链接、喜马拉雅登录辅助。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..config import Config

log = logging.getLogger("web")
router = APIRouter()


def _ctx(request: Request):
    return request.app.state.ctx


@router.get("/api/health")
async def health(request: Request):
    ctx = _ctx(request)
    return {
        "status": "ok",
        "accounts": len(ctx.cfg.douyin.accounts),
        "album_id": ctx.cfg.ximalaya.album_id or None,
        "last_poll": ctx.pipeline.last_poll,
    }


@router.get("/api/records")
async def records(request: Request, limit: int = 200):
    ctx = _ctx(request)
    return {"records": ctx.store.list_records(limit=min(limit, 1000)),
            "stats": ctx.store.stats()}


@router.post("/api/poll")
async def trigger_poll(request: Request):
    ctx = _ctx(request)
    if ctx.pipeline.last_poll.get("running"):
        raise HTTPException(409, "已有轮询进行中")
    result = await ctx.pipeline.poll_once()
    return result


class SubmitBody(BaseModel):
    url: str


@router.post("/api/submit")
async def submit(body: SubmitBody, request: Request):
    ctx = _ctx(request)
    try:
        return await ctx.pipeline.submit_url(body.url)
    except NotImplementedError as e:
        raise HTTPException(501, str(e))
    except Exception as e:
        log.exception("手动提交失败")
        raise HTTPException(400, f"提交失败：{e}")


@router.post("/api/login/start")
async def login_start(request: Request):
    ctx = _ctx(request)
    sess = getattr(ctx, "login_session", None)
    if sess and sess.running:
        return sess.status()
    from ..ximalaya.login import start_login_task

    ctx.login_session = start_login_task(ctx.cfg)
    return ctx.login_session.status()


@router.get("/api/login/status")
async def login_status(request: Request):
    sess = getattr(_ctx(request), "login_session", None)
    if sess:
        return sess.status()
    return {"running": False, "logged_in": False, "message": "", "qr": None}


@router.get("/login_qr.png")
async def login_qr(request: Request):
    from pathlib import Path

    p = Path(_ctx(request).cfg.data_dir) / "login_qr.png"
    if not p.exists():
        raise HTTPException(404, "暂无二维码，请先在状态页发起登录")
    return FileResponse(p, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/api/config")
async def config_view(request: Request):
    ctx: Config = _ctx(request).cfg
    return {
        "poll_interval_minutes": ctx.poll_interval_minutes,
        "accounts": [
            {"name": a.name, "sec_uid": a.sec_uid} for a in ctx.douyin.accounts
        ],
        "audio": {"format": ctx.audio.format, "bitrate": ctx.audio.bitrate},
        "ximalaya": {
            "album_id": ctx.ximalaya.album_id or None,
            "visibility": ctx.ximalaya.visibility,
        },
        "keep_video": ctx.keep_video,
        "max_attempts": ctx.max_attempts,
    }
