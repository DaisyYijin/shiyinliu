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


# ---- 登录状态（会话任务优先，其次读登录标记文件恢复显示） ----
def _auth_from_file(request: Request, filename: str) -> dict | None:
    import json
    from pathlib import Path

    p = Path(_ctx(request).cfg.data_dir) / filename
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _logged_in_from_auth(auth: dict | None) -> dict:
    if auth:
        return {
            "running": False,
            "logged_in": True,
            "message": "已登录（沿用重启前登录态）",
            "account": auth.get("account") or None,
            "avatar": auth.get("avatar") or None,
            "qr": None,
        }
    return {"running": False, "logged_in": False, "message": "",
            "account": None, "avatar": None, "qr": None}


@router.get("/api/login/status")
async def login_status(request: Request):
    sess = getattr(_ctx(request), "login_session", None)
    if sess and (sess.running or sess.logged_in):
        return sess.status()
    return _logged_in_from_auth(_auth_from_file(request, "ximalaya_auth.json"))


@router.post("/api/login/start")
async def login_start(request: Request):
    ctx = _ctx(request)
    sess = getattr(ctx, "login_session", None)
    if sess and sess.running:
        return sess.status()
    from ..ximalaya.login import start_login_task

    ctx.login_session = start_login_task(ctx.cfg)
    return ctx.login_session.status()


@router.get("/login_qr.png")
async def login_qr(request: Request):
    from pathlib import Path

    p = Path(_ctx(request).cfg.data_dir) / "login_qr.png"
    if not p.exists():
        raise HTTPException(404, "暂无二维码，请先在状态页发起登录")
    return FileResponse(p, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/api/douyin/login/status")
async def douyin_login_status(request: Request):
    sess = getattr(_ctx(request), "douyin_login_session", None)
    if sess and (sess.running or sess.logged_in):
        return sess.status()
    return _logged_in_from_auth(_auth_from_file(request, "douyin_auth.json"))


@router.post("/api/douyin/login/start")
async def douyin_login_start(request: Request):
    ctx = _ctx(request)
    sess = getattr(ctx, "douyin_login_session", None)
    if sess and sess.running:
        return sess.status()
    from ..douyin.login import start_douyin_login_task

    ctx.douyin_login_session = start_douyin_login_task(ctx.cfg)
    return ctx.douyin_login_session.status()


class VerifyBody(BaseModel):
    value: str


@router.post("/api/douyin/login/verify")
async def douyin_login_verify(body: VerifyBody, request: Request):
    sess = getattr(_ctx(request), "douyin_login_session", None)
    if not sess or not sess.pending_verify:
        raise HTTPException(409, "当前没有等待输入的验证")
    ok = sess.submit_verify(body.value)
    return {"ok": ok}


@router.get("/douyin_qr.png")
async def douyin_qr(request: Request):
    from pathlib import Path

    p = Path(_ctx(request).cfg.data_dir) / "douyin_qr.png"
    if not p.exists():
        raise HTTPException(404, "暂无二维码，请先发起抖音登录")
    return FileResponse(p, media_type="image/png", headers={"Cache-Control": "no-store"})


# ---- 监控账号管理（配置文件 + 状态页搜索添加） ----
@router.get("/api/accounts")
async def list_accounts(request: Request):
    return {"accounts": _ctx(request).store.list_accounts()}


@router.get("/api/accounts/search")
async def search_accounts(request: Request, q: str):
    ctx = _ctx(request)
    if not (q or "").strip():
        raise HTTPException(400, "搜索关键词为空")
    from ..douyin.search import search_creators

    results = await search_creators(ctx.cfg, q)
    return {"results": results}


class AccountBody(BaseModel):
    sec_uid: str
    name: str = ""


@router.post("/api/accounts")
async def add_account(body: AccountBody, request: Request):
    ctx = _ctx(request)
    from ..config import extract_sec_uid

    sec_uid = extract_sec_uid(body.sec_uid) or body.sec_uid.strip()
    if not sec_uid.startswith("MS4wLjABAAAA"):
        raise HTTPException(400, "sec_uid 格式不正确")
    ctx.store.upsert_account(sec_uid, (body.name or "").strip() or "未命名博主")
    await ctx.scheduler.poll_now()  # 唤醒调度循环：立即开始首轮全量拉取
    return {"ok": True, "sec_uid": sec_uid}


@router.delete("/api/accounts/{sec_uid}")
async def remove_account(sec_uid: str, request: Request):
    ok = _ctx(request).store.delete_account(sec_uid)
    if not ok:
        raise HTTPException(403, "仅可删除通过搜索添加的账号；配置文件来源请修改 app.yaml")
    return {"ok": True}


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
