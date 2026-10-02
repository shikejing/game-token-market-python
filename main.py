import uvicorn
from fastapi import FastAPI

from app.api import router as market_router
from app.service import get_service

app = FastAPI(title="Game Token Market")
app.include_router(market_router)


@app.on_event("startup")
def _startup() -> None:
    get_service().start()


@app.get("/")
def read_root():
    return {"Hello": "World"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
