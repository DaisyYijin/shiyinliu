"""python -m app 启动服务。"""
from __future__ import annotations

import uvicorn

from .config import load_config


def main() -> None:
    cfg = load_config()
    uvicorn.run("app.main:app", host="0.0.0.0", port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
