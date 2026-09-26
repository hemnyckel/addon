"""Run the Hemnyckel relay (development / standalone)."""
from __future__ import annotations

import uvicorn

from app.config import load_config
from app.main import create_app

if __name__ == "__main__":
    cfg = load_config()
    uvicorn.run(create_app(cfg), host="0.0.0.0", port=cfg.port)
