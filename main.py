from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from app.cart.router import router as cart_router
from app.core.redis import get_redis_client
from app.transaction.router import router as transaction_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_redis_client()
    yield
    await get_redis_client().aclose()


app = FastAPI(title="B2C E-commerce API", version="4.0", lifespan=lifespan)

app.include_router(transaction_router, prefix="/api/v1")
app.include_router(cart_router, prefix="/api/v1")


@app.get("/health", tags=["Health"])
async def health_check() -> dict:
    return {"status": "ok"}


@app.get("/dashboard", response_class=HTMLResponse, tags=["Dashboard"])
async def get_dashboard() -> str:
    html_path = Path(__file__).parent / "dashboard.html"
    return html_path.read_text(encoding="utf-8")
