#!/usr/bin/env python3
"""Run xo-lite.

    python run.py                 # http://127.0.0.1:5002
    XO_LITE_PROJECTS=~/code python run.py
    XO_LITE_ENGINE=rules python run.py

Point xo-coworker at it with its default XO_API and the two line up:

    cd ../xo-coworker && python serve.py      # :5180 proxies /api/* to :5002
"""

from __future__ import annotations

import logging

import uvicorn

from xolite import config

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    print(f"xo-lite on http://{config.HOST}:{config.PORT}")
    print(f"  projects: {config.PROJECTS_ROOT}")
    print(f"  store:    {config.HOME}")
    print(f"  engine:   {config.ENGINE}")
    uvicorn.run("xolite.app:app", host=config.HOST, port=config.PORT, log_level="info")
