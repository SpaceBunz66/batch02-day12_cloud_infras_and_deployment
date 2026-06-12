"""Sliding-window rate limiting, Redis-backed when available."""
import time
from collections import defaultdict, deque

from fastapi import Depends, HTTPException

from app.auth import verify_api_key
from app.config import settings

try:
    import redis
except ImportError:  # pragma: no cover
    redis = None


_windows: dict[str, deque] = defaultdict(deque)
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


def check_rate_limit(user_id: str = Depends(verify_api_key)) -> None:
    limit = settings.rate_limit_per_minute
    window_seconds = 60
    now = time.time()
    client = _client()

    if client:
        key = f"rate:{user_id}"
        cutoff = now - window_seconds
        pipe = client.pipeline()
        pipe.zremrangebyscore(key, 0, cutoff)
        pipe.zcard(key)
        _, count = pipe.execute()
        if count >= limit:
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded: {limit} req/min",
                headers={"Retry-After": "60"},
            )
        client.zadd(key, {str(now): now})
        client.expire(key, window_seconds + 5)
        return None

    bucket = _windows[user_id]
    while bucket and bucket[0] < now - window_seconds:
        bucket.popleft()
    if len(bucket) >= limit:
        retry_after = int(bucket[0] + window_seconds - now) + 1
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: {limit} req/min",
            headers={"Retry-After": str(retry_after)},
        )
    bucket.append(now)
    return None
