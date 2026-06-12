"""Monthly budget guard for LLM usage."""
from datetime import datetime, timezone

from fastapi import Depends, HTTPException

from app.auth import verify_api_key
from app.config import settings

try:
    import redis
except ImportError:  # pragma: no cover
    redis = None


PRICE_PER_1K_INPUT_TOKENS = 0.00015
PRICE_PER_1K_OUTPUT_TOKENS = 0.0006
_usage_memory: dict[str, dict] = {}
_redis_client = None


def _client():
    global _redis_client
    if not settings.redis_url or redis is None:
        return None
    if _redis_client is not None:
        return _redis_client
    try:
        _redis_client = redis.from_url(settings.redis_url, decode_responses=True)
        _redis_client.ping()
        return _redis_client
    except Exception:
        _redis_client = None
        return None


def _key(user_id: str) -> str:
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    return f"budget:{user_id}:{month}"


def _cost(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens / 1000 * PRICE_PER_1K_INPUT_TOKENS
        + output_tokens / 1000 * PRICE_PER_1K_OUTPUT_TOKENS
    )


def get_usage(user_id: str) -> dict:
    key = _key(user_id)
    client = _client()
    if client:
        raw = client.hgetall(key)
        cost_usd = float(raw.get("cost_usd", 0) or 0)
        return {
            "user_id": user_id,
            "cost_usd": round(cost_usd, 6),
            "monthly_budget_usd": settings.monthly_budget_usd,
            "budget_remaining_usd": round(max(0, settings.monthly_budget_usd - cost_usd), 6),
            "requests": int(raw.get("requests", 0) or 0),
            "input_tokens": int(raw.get("input_tokens", 0) or 0),
            "output_tokens": int(raw.get("output_tokens", 0) or 0),
        }

    usage = _usage_memory.get(key, {})
    cost_usd = float(usage.get("cost_usd", 0))
    return {
        "user_id": user_id,
        "cost_usd": round(cost_usd, 6),
        "monthly_budget_usd": settings.monthly_budget_usd,
        "budget_remaining_usd": round(max(0, settings.monthly_budget_usd - cost_usd), 6),
        "requests": int(usage.get("requests", 0)),
        "input_tokens": int(usage.get("input_tokens", 0)),
        "output_tokens": int(usage.get("output_tokens", 0)),
    }


def check_budget(user_id: str = Depends(verify_api_key)) -> None:
    usage = get_usage(user_id)
    if usage["cost_usd"] >= settings.monthly_budget_usd:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "Monthly budget exceeded",
                "used_usd": usage["cost_usd"],
                "budget_usd": settings.monthly_budget_usd,
            },
        )
    return None


def record_usage(user_id: str, input_tokens: int, output_tokens: int) -> dict:
    key = _key(user_id)
    cost_usd = _cost(input_tokens, output_tokens)
    client = _client()
    if client:
        pipe = client.pipeline()
        pipe.hincrby(key, "requests", 1)
        pipe.hincrby(key, "input_tokens", input_tokens)
        pipe.hincrby(key, "output_tokens", output_tokens)
        pipe.hincrbyfloat(key, "cost_usd", cost_usd)
        pipe.expire(key, 32 * 24 * 3600)
        pipe.execute()
        return get_usage(user_id)

    usage = _usage_memory.setdefault(
        key,
        {"requests": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
    )
    usage["requests"] += 1
    usage["input_tokens"] += input_tokens
    usage["output_tokens"] += output_tokens
    usage["cost_usd"] += cost_usd
    return get_usage(user_id)
