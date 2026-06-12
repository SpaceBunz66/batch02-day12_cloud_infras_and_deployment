"""
Production AI Agent - combines the Day 12 production concepts.

Includes config from env vars, API key auth, rate limiting, cost guard,
conversation history, health/readiness checks, JSON logging, and graceful
shutdown.
"""
import json
import logging
import signal
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn

from app.auth import verify_api_key
from app.config import settings
from app.cost_guard import check_budget, get_usage, record_usage
from app.rate_limiter import check_rate_limit
from utils.mock_llm import ask as llm_ask

try:
    import redis
except ImportError:  # pragma: no cover - dependency is installed in lab env
    redis = None


logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format='{"ts":"%(asctime)s","lvl":"%(levelname)s","msg":"%(message)s"}',
)
logger = logging.getLogger(__name__)

START_TIME = time.time()
_is_ready = False
_request_count = 0
_error_count = 0
_redis_client = None
_memory_history: dict[str, list[dict]] = {}


def redis_client():
    """Return a Redis client when REDIS_URL is configured and reachable."""
    global _redis_client
    if not settings.redis_url or redis is None:
        return None
    if _redis_client is not None:
        return _redis_client
    try:
        _redis_client = redis.from_url(settings.redis_url, decode_responses=True)
        _redis_client.ping()
        return _redis_client
    except Exception as exc:
        logger.warning(json.dumps({"event": "redis_unavailable", "error": str(exc)}))
        _redis_client = None
        return None


def load_history(user_id: str) -> list[dict]:
    client = redis_client()
    key = f"history:{user_id}"
    if client:
        return [json.loads(item) for item in client.lrange(key, 0, -1)]
    return _memory_history.get(key, [])


def append_history(user_id: str, role: str, content: str) -> list[dict]:
    key = f"history:{user_id}"
    message = {
        "role": role,
        "content": content,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    client = redis_client()
    if client:
        client.rpush(key, json.dumps(message))
        client.ltrim(key, -20, -1)
        client.expire(key, 24 * 3600)
        return [json.loads(item) for item in client.lrange(key, 0, -1)]

    history = _memory_history.setdefault(key, [])
    history.append(message)
    _memory_history[key] = history[-20:]
    return _memory_history[key]


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _is_ready
    logger.info(json.dumps({
        "event": "startup",
        "app": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
    }))
    redis_client()
    _is_ready = True
    logger.info(json.dumps({"event": "ready"}))

    yield

    _is_ready = False
    logger.info(json.dumps({"event": "shutdown"}))


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/docs" if settings.environment != "production" else None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)


@app.middleware("http")
async def request_middleware(request: Request, call_next):
    global _request_count, _error_count
    start = time.time()
    _request_count += 1
    try:
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers.pop("server", None)
        logger.info(json.dumps({
            "event": "request",
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "ms": round((time.time() - start) * 1000, 1),
        }))
        return response
    except Exception:
        _error_count += 1
        raise


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    user_id: str | None = Field(default=None, max_length=100)


class AskResponse(BaseModel):
    question: str
    answer: str
    user_id: str
    model: str
    history_turns: int
    usage: dict
    timestamp: str


@app.get("/", tags=["Info"])
def root():
    return {
        "app": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "endpoints": {
            "ask": "POST /ask (requires X-API-Key)",
            "health": "GET /health",
            "ready": "GET /ready",
            "metrics": "GET /metrics (requires X-API-Key)",
        },
    }


@app.post("/ask", response_model=AskResponse, tags=["Agent"])
async def ask_agent(
    body: AskRequest,
    request: Request,
    api_user_id: str = Depends(verify_api_key),
    _rate_limit: None = Depends(check_rate_limit),
    _budget: None = Depends(check_budget),
):
    user_id = body.user_id or api_user_id
    append_history(user_id, "user", body.question)

    logger.info(json.dumps({
        "event": "agent_call",
        "q_len": len(body.question),
        "user_id": user_id,
        "client": str(request.client.host) if request.client else "unknown",
    }))

    answer = llm_ask(body.question)
    history = append_history(user_id, "assistant", answer)

    input_tokens = len(body.question.split()) * 2
    output_tokens = len(answer.split()) * 2
    usage = record_usage(api_user_id, input_tokens, output_tokens)

    return AskResponse(
        question=body.question,
        answer=answer,
        user_id=user_id,
        model=settings.llm_model,
        history_turns=len([msg for msg in history if msg["role"] == "user"]),
        usage=usage,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


@app.get("/history/{user_id}", tags=["Agent"])
def history(user_id: str, _api_user_id: str = Depends(verify_api_key)):
    messages = load_history(user_id)
    return {"user_id": user_id, "messages": messages, "count": len(messages)}


@app.get("/health", tags=["Operations"])
def health():
    client = redis_client()
    return {
        "status": "ok",
        "version": settings.app_version,
        "environment": settings.environment,
        "uptime_seconds": round(time.time() - START_TIME, 1),
        "total_requests": _request_count,
        "checks": {
            "llm": "mock" if not settings.openai_api_key else settings.llm_model,
            "storage": "redis" if client else "in-memory",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/ready", tags=["Operations"])
def ready():
    if not _is_ready:
        raise HTTPException(503, "Not ready")
    if settings.redis_url and not redis_client():
        raise HTTPException(503, "Redis not available")
    return {"ready": True, "storage": "redis" if redis_client() else "in-memory"}


@app.get("/metrics", tags=["Operations"])
def metrics(api_user_id: str = Depends(verify_api_key)):
    usage = get_usage(api_user_id)
    return {
        "uptime_seconds": round(time.time() - START_TIME, 1),
        "total_requests": _request_count,
        "error_count": _error_count,
        "usage": usage,
    }


def _handle_signal(signum, _frame):
    logger.info(json.dumps({"event": "signal", "signum": signum}))


signal.signal(signal.SIGTERM, _handle_signal)


if __name__ == "__main__":
    logger.info(f"Starting {settings.app_name} on {settings.host}:{settings.port}")
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        timeout_graceful_shutdown=30,
    )
