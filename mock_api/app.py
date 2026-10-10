"""Mock downstream dependency for n8n workflows. Delay/error rate are set at runtime."""
import asyncio
import random

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI()


class Settings(BaseModel):
    delay_s: float = 4.0
    error_rate: float = 0.0


settings = Settings()


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.api_route("/work", methods=["GET", "POST"])
async def work(request_id: str | None = None):
    await asyncio.sleep(settings.delay_s)
    if random.random() < settings.error_rate:
        raise HTTPException(status_code=500, detail="injected mock error")
    return {"ok": True, "request_id": request_id}


@app.get("/admin")
async def get_admin() -> Settings:
    return settings


@app.post("/admin")
async def set_admin(new: Settings) -> Settings:
    settings.delay_s = max(0.0, min(new.delay_s, 60.0))
    settings.error_rate = max(0.0, min(new.error_rate, 1.0))
    return settings
