"""
Redis Caching Layer for Billing Service - Production Scale

Provides caching for frequently accessed data to handle millions of users.
Uses Redis with automatic fallback to in-memory cache.
"""

import json
import logging
import hashlib
from datetime import datetime
from typing import Any, Dict, Optional, Callable
from functools import wraps

from .config import settings

logger = logging.getLogger(__name__)

# Try to import Redis
try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    logger.warning("redis not installed - using in-memory cache")


class CacheConfig:
    """Cache configuration with TTLs for different data types."""
    
    # Dashboard data - changes frequently but can tolerate 30s staleness
    DASHBOARD_TTL = 30  # seconds
    
    # Subscription data - changes rarely, cache longer
    SUBSCRIPTION_TTL = 300  # 5 minutes
    
    # Credit balance - needs to be fresh but can cache briefly
    CREDIT_BALANCE_TTL = 10  # seconds
    
    # Usage breakdown - computed data, cache longer
    BREAKDOWN_TTL = 60  # 1 minute
    
    # Pricing data - rarely changes
    PRICING_TTL = 3600  # 1 hour
    
    # User plan - changes rarely
    USER_PLAN_TTL = 300  # 5 minutes


class BillingCache:
    """
    Redis-backed cache for billing data.
    
    Features:
    - Automatic Redis connection with fallback
    - TTL-based expiration
    - Cache invalidation on writes
    - Metrics tracking
    """
    
    def __init__(self, redis_url: Optional[str] = None):
        self.redis_client = None
        self.redis_url = redis_url or settings.REDIS_URL
        self._memory_cache: Dict[str, tuple] = {}  # key -> (value, expires_at)
        self._stats = {
            "hits": 0,
            "misses": 0,
            "errors": 0,
        }
        
        if self.redis_url and REDIS_AVAILABLE:
            try:
                self.redis_client = redis.from_url(
                    self.redis_url,
                    encoding="utf-8",
                    decode_responses=True,
                )
                logger.info(f"Redis cache connected: {self.redis_url}")
            except Exception as e:
                logger.error(f"Failed to connect to Redis: {e}")
    
    def _make_key(self, prefix: str, *args) -> str:
        """Generate cache key from prefix and arguments."""
        key_data = ":".join(str(a) for a in args)
        return f"billing:{prefix}:{key_data}"
    
    async def get(self, key: str) -> Optional[Any]:
        """Get value from cache."""
        # Try Redis first
        if self.redis_client:
            try:
                value = await self.redis_client.get(key)
                if value:
                    self._stats["hits"] += 1
                    return json.loads(value)
                self._stats["misses"] += 1
                return None
            except Exception as e:
                logger.error(f"Redis get error: {e}")
                self._stats["errors"] += 1
        
        # Fallback to memory cache
        if key in self._memory_cache:
            value, expires_at = self._memory_cache[key]
            if datetime.utcnow().timestamp() < expires_at:
                self._stats["hits"] += 1
                return value
            else:
                del self._memory_cache[key]
        
        self._stats["misses"] += 1
        return None
    
    async def set(self, key: str, value: Any, ttl: int = 60) -> bool:
        """Set value in cache with TTL."""
        # Try Redis first
        if self.redis_client:
            try:
                await self.redis_client.setex(
                    key,
                    ttl,
                    json.dumps(value, default=str)
                )
                return True
            except Exception as e:
                logger.error(f"Redis set error: {e}")
                self._stats["errors"] += 1
        
        # Fallback to memory cache
        expires_at = datetime.utcnow().timestamp() + ttl
        self._memory_cache[key] = (value, expires_at)
        return True
    
    async def delete(self, key: str) -> bool:
        """Delete key from cache."""
        if self.redis_client:
            try:
                await self.redis_client.delete(key)
            except Exception as e:
                logger.error(f"Redis delete error: {e}")
        
        if key in self._memory_cache:
            del self._memory_cache[key]
        
        return True
    
    async def invalidate_user(self, user_id: str):
        """Invalidate all cache entries for a user."""
        patterns = [
            f"billing:dashboard:{user_id}",
            f"billing:subscription:{user_id}",
            f"billing:credits:{user_id}",
            f"billing:breakdown:{user_id}",
            f"billing:plan:{user_id}",
        ]
        
        for pattern in patterns:
            await self.delete(pattern)
        
        logger.debug(f"Invalidated cache for user: {user_id}")
    
    async def get_dashboard(self, user_id: str) -> Optional[Dict]:
        """Get cached dashboard data."""
        key = self._make_key("dashboard", user_id)
        return await self.get(key)
    
    async def set_dashboard(self, user_id: str, data: Dict):
        """Cache dashboard data."""
        key = self._make_key("dashboard", user_id)
        await self.set(key, data, CacheConfig.DASHBOARD_TTL)
    
    async def get_subscription(self, user_id: str) -> Optional[Dict]:
        """Get cached subscription data."""
        key = self._make_key("subscription", user_id)
        return await self.get(key)
    
    async def set_subscription(self, user_id: str, data: Dict):
        """Cache subscription data."""
        key = self._make_key("subscription", user_id)
        await self.set(key, data, CacheConfig.SUBSCRIPTION_TTL)
    
    async def get_credits(self, user_id: str) -> Optional[Dict]:
        """Get cached credit balance."""
        key = self._make_key("credits", user_id)
        return await self.get(key)
    
    async def set_credits(self, user_id: str, data: Dict):
        """Cache credit balance."""
        key = self._make_key("credits", user_id)
        await self.set(key, data, CacheConfig.CREDIT_BALANCE_TTL)
    
    async def get_breakdown(self, user_id: str) -> Optional[Dict]:
        """Get cached usage breakdown."""
        key = self._make_key("breakdown", user_id)
        return await self.get(key)
    
    async def set_breakdown(self, user_id: str, data: Dict):
        """Cache usage breakdown."""
        key = self._make_key("breakdown", user_id)
        await self.set(key, data, CacheConfig.BREAKDOWN_TTL)
    
    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        total = self._stats["hits"] + self._stats["misses"]
        hit_rate = self._stats["hits"] / total if total > 0 else 0
        
        return {
            **self._stats,
            "total_requests": total,
            "hit_rate": round(hit_rate * 100, 2),
            "redis_connected": self.redis_client is not None,
            "memory_cache_size": len(self._memory_cache),
        }
    
    async def health_check(self) -> Dict[str, Any]:
        """Check cache health."""
        redis_ok = False
        
        if self.redis_client:
            try:
                await self.redis_client.ping()
                redis_ok = True
            except Exception as e:
                logger.error(f"Redis health check failed: {e}")
        
        return {
            "status": "healthy" if redis_ok or not REDIS_AVAILABLE else "degraded",
            "redis_connected": redis_ok,
            "stats": self.get_stats(),
        }


# Global cache instance
_cache: Optional[BillingCache] = None


def get_cache() -> BillingCache:
    """Get the global cache instance."""
    global _cache
    if _cache is None:
        _cache = BillingCache()
    return _cache


async def init_cache():
    """Initialize the cache."""
    global _cache
    _cache = BillingCache()
    logger.info("Billing cache initialized")


async def shutdown_cache():
    """Shutdown the cache."""
    global _cache
    if _cache and _cache.redis_client:
        await _cache.redis_client.close()
    _cache = None
    logger.info("Billing cache shutdown")


def cached(prefix: str, ttl: int = 60, key_arg: str = "user_id"):
    """
    Decorator for caching function results.
    
    Usage:
        @cached("dashboard", ttl=30, key_arg="user_id")
        async def get_dashboard_data(user_id: str, ...):
            ...
    """
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            cache = get_cache()
            
            # Extract cache key from arguments
            cache_key_value = kwargs.get(key_arg)
            if not cache_key_value and args:
                # Try to get from positional args
                import inspect
                sig = inspect.signature(func)
                params = list(sig.parameters.keys())
                if key_arg in params:
                    idx = params.index(key_arg)
                    if idx < len(args):
                        cache_key_value = args[idx]
            
            if not cache_key_value:
                # Can't cache without key, just call function
                return await func(*args, **kwargs)
            
            key = cache._make_key(prefix, cache_key_value)
            
            # Try cache first
            cached_value = await cache.get(key)
            if cached_value is not None:
                return cached_value
            
            # Call function and cache result
            result = await func(*args, **kwargs)
            await cache.set(key, result, ttl)
            
            return result
        
        return wrapper
    return decorator
