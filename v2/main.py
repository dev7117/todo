"""Entrypoint: python main.py serves v2 on TODO_V2_PORT (default 7680)."""
import uvicorn

from todo_v2 import config
from todo_v2.app import create_app

if __name__ == "__main__":
    cfg = config.load()
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port)
