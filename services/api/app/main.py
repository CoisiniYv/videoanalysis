import os

import psycopg
import redis.asyncio as redis
from fastapi import FastAPI, HTTPException

app = FastAPI(title="Phase 0 API")

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://video:video@postgres:5432/video_analytics",
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
async def ready() -> dict[str, object]:
    checks: dict[str, bool] = {"redis": False, "postgres": False}

    redis_client = redis.from_url(
        REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
    )
    try:
        await redis_client.ping()
        checks["redis"] = True
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={"status": "not_ready", "checks": checks, "error": str(exc)},
        ) from exc
    finally:
        await redis_client.aclose()

    try:
        async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")
                await cur.fetchone()
        checks["postgres"] = True
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={"status": "not_ready", "checks": checks, "error": str(exc)},
        ) from exc

    return {"status": "ready", "checks": checks}
