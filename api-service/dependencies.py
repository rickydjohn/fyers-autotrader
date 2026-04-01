from typing import AsyncGenerator
import redis.asyncio as aioredis
from fastapi import Request


async def get_redis(request: Request) -> AsyncGenerator[aioredis.Redis, None]:
    return request.app.state.redis
