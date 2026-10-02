"""Game token market service entry point.

Starts the FastAPI app, loads (or creates) the market engine backed by
local durable storage, and serves the HTTP API.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from market.api import router as market_router, set_engine
from market.engine import MarketEngine

def _data_dir() -> str:
    return os.environ.get(
        "MARKET_DATA_DIR", os.path.join(os.path.dirname(__file__), "data")
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Read at startup so tests can override via MARKET_DATA_DIR.
    engine = MarketEngine(_data_dir())
    engine.load()
    set_engine(engine)
    yield


app = FastAPI(title="Game Token Market", lifespan=lifespan)
app.include_router(market_router)


@app.get("/")
def read_root():
    return {"Hello": "World"}


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
