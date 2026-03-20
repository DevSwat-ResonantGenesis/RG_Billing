"""
Rate Limiting Service - Phase 4.1 GTM

Protect billing endpoints from abuse with configurable rate limits.
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)

# Try to import Redis
try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    logger.warning("redis not installed - using in-memory rate limiting")


class RateLimitTier(str, Enum):
    """Rate limit tiers."""
    DEVELOPER = "developer"
    PLUS = "plus"
    ENTERPRISE = "enterprise"
    INTERNAL = "internal"


# Rate limits by tier (requests per minute)
RATE_LIMITS = {
    RateLimitTier.DEVELOPER: {
        "default": 60,
        "credit_deduct": 100,
        "credit_check": 200,
        "webhook": 50,
        "dashboard": 30,
        "invoice": 10,
    },
    RateLimitTier.PLUS: {
        "default": 300,
        "credit_deduct": 500,
        "credit_check": 1000,
        "webhook": 200,
        "dashboard": 100,
        "invoice": 50,
    },
    RateLimitTier.ENTERPRISE: {
        "default": 1000,
        "credit_deduct": 2000,
        "credit_check": 5000,
        "webhook": 500,
        "dashboard": 300,
        "invoice": 200,
    },
    RateLimitTier.INTERNAL: {
        "default": 10000,
        "credit_deduct": 10000,
        "credit_check": 10000,
        "webhook": 10000,
        "dashboard": 10000,
        "invoice": 10000,
    },
}


@dataclass
class RateLimitResult:
    """Result of rate limit check."""
    allowed: bool
    remaining: int
    limit: int
    reset_at: int  # Unix timestamp
    retry_after: Optional[int] = None  # Seconds until retry
    
    def to_headers(self) -> Dict[str, str]:
        """Convert to HTTP headers."""
        headers = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
            "X-RateLimit-Reset": str(self.reset_at),
        }
        if self.retry_after:
            headers["Retry-After"] = str(self.retry_after)
        return headers


class RateLimiter:
    """
    Rate limiting service using sliding window algorithm.
    
    Features:
    - Tier-based rate limits
    - Per-endpoint limits
    - Redis-backed for distributed systems
    - In-memory fallback
    - Sliding window for smooth limiting
    """
    
    def __init__(self, redis_client=None, window_seconds: int = 60):
        """
        Initialize rate limiter.
        
        Args:
            redis_client: Optional Redis client
            window_seconds: Rate limit window in seconds
        """
        self.redis = redis_client
        self.window_seconds = window_seconds
        
        # In-memory fallback: {key: [(timestamp, count), ...]}
        self._memory_store: Dict[str, list] = {}
        self._last_cleanup = time.time()
    
    def _get_key(self, identifier: str, endpoint: str) -> str:
        """Generate rate limit key."""
        return f"ratelimit:{identifier}:{endpoint}"
    
    def _get_limit(self, tier: RateLimitTier, endpoint: str) -> int:
        """Get rate limit for tier and endpoint."""
        tier_limits = RATE_LIMITS.get(tier, RATE_LIMITS[RateLimitTier.FREE])
        return tier_limits.get(endpoint, tier_limits["default"])
    
    async def check(
        self,
        identifier: str,
        endpoint: str = "default",
        tier: RateLimitTier = RateLimitTier.FREE,
    ) -> RateLimitResult:
        """
        Check if request is allowed.
        
        Args:
            identifier: User ID or IP address
            endpoint: Endpoint being accessed
            tier: User's rate limit tier
            
        Returns:
            RateLimitResult with allowed status
        """
        limit = self._get_limit(tier, endpoint)
        key = self._get_key(identifier, endpoint)
        now = time.time()
        window_start = now - self.window_seconds
        reset_at = int(now + self.window_seconds)
        
        if self.redis and REDIS_AVAILABLE:
            return await self._check_redis(key, limit, now, window_start, reset_at)
        else:
            return self._check_memory(key, limit, now, window_start, reset_at)
    
    async def _check_redis(
        self,
        key: str,
        limit: int,
        now: float,
        window_start: float,
        reset_at: int,
    ) -> RateLimitResult:
        """Check rate limit using Redis."""
        try:
            pipe = self.redis.pipeline()
            
            # Remove old entries
            pipe.zremrangebyscore(key, 0, window_start)
            
            # Count current entries
            pipe.zcard(key)
            
            # Add new entry
            pipe.zadd(key, {str(now): now})
            
            # Set expiry
            pipe.expire(key, self.window_seconds * 2)
            
            results = await pipe.execute()
            current_count = results[1]
            
            if current_count >= limit:
                # Get oldest entry to calculate retry time
                oldest = await self.redis.zrange(key, 0, 0, withscores=True)
                if oldest:
                    retry_after = int(oldest[0][1] + self.window_seconds - now) + 1
                else:
                    retry_after = self.window_seconds
                
                return RateLimitResult(
                    allowed=False,
                    remaining=0,
                    limit=limit,
                    reset_at=reset_at,
                    retry_after=max(1, retry_after),
                )
            
            return RateLimitResult(
                allowed=True,
                remaining=limit - current_count - 1,
                limit=limit,
                reset_at=reset_at,
            )
            
        except Exception as e:
            logger.error(f"Redis rate limit error: {e}")
            # Fail open on Redis errors
            return RateLimitResult(
                allowed=True,
                remaining=limit,
                limit=limit,
                reset_at=reset_at,
            )
    
    def _check_memory(
        self,
        key: str,
        limit: int,
        now: float,
        window_start: float,
        reset_at: int,
    ) -> RateLimitResult:
        """Check rate limit using in-memory store."""
        # Periodic cleanup
        if now - self._last_cleanup > 60:
            self._cleanup_memory()
            self._last_cleanup = now
        
        # Get or create entry list
        if key not in self._memory_store:
            self._memory_store[key] = []
        
        entries = self._memory_store[key]
        
        # Remove old entries
        entries[:] = [(ts, c) for ts, c in entries if ts > window_start]
        
        # Count current requests
        current_count = sum(c for _, c in entries)
        
        if current_count >= limit:
            # Calculate retry time
            if entries:
                oldest_ts = min(ts for ts, _ in entries)
                retry_after = int(oldest_ts + self.window_seconds - now) + 1
            else:
                retry_after = self.window_seconds
            
            return RateLimitResult(
                allowed=False,
                remaining=0,
                limit=limit,
                reset_at=reset_at,
                retry_after=max(1, retry_after),
            )
        
        # Add new entry
        entries.append((now, 1))
        
        return RateLimitResult(
            allowed=True,
            remaining=limit - current_count - 1,
            limit=limit,
            reset_at=reset_at,
        )
    
    def _cleanup_memory(self):
        """Clean up old entries from memory store."""
        cutoff = time.time() - self.window_seconds * 2
        keys_to_delete = []
        
        for key, entries in self._memory_store.items():
            entries[:] = [(ts, c) for ts, c in entries if ts > cutoff]
            if not entries:
                keys_to_delete.append(key)
        
        for key in keys_to_delete:
            del self._memory_store[key]
    
    async def reset(self, identifier: str, endpoint: str = "default"):
        """
        Reset rate limit for an identifier.
        
        Args:
            identifier: User ID or IP address
            endpoint: Endpoint to reset
        """
        key = self._get_key(identifier, endpoint)
        
        if self.redis and REDIS_AVAILABLE:
            await self.redis.delete(key)
        elif key in self._memory_store:
            del self._memory_store[key]
    
    async def get_status(
        self,
        identifier: str,
        endpoint: str = "default",
        tier: RateLimitTier = RateLimitTier.FREE,
    ) -> Dict[str, Any]:
        """
        Get current rate limit status without incrementing.
        
        Args:
            identifier: User ID or IP address
            endpoint: Endpoint to check
            tier: User's rate limit tier
            
        Returns:
            Current rate limit status
        """
        limit = self._get_limit(tier, endpoint)
        key = self._get_key(identifier, endpoint)
        now = time.time()
        window_start = now - self.window_seconds
        
        if self.redis and REDIS_AVAILABLE:
            try:
                await self.redis.zremrangebyscore(key, 0, window_start)
                current_count = await self.redis.zcard(key)
            except:
                current_count = 0
        else:
            entries = self._memory_store.get(key, [])
            entries = [(ts, c) for ts, c in entries if ts > window_start]
            current_count = sum(c for _, c in entries)
        
        return {
            "identifier": identifier,
            "endpoint": endpoint,
            "tier": tier.value,
            "limit": limit,
            "used": current_count,
            "remaining": max(0, limit - current_count),
            "window_seconds": self.window_seconds,
        }


# Global instance
rate_limiter = RateLimiter()


# ============================================
# MIDDLEWARE HELPERS
# ============================================

async def check_rate_limit(
    identifier: str,
    endpoint: str = "default",
    tier: str = "developer",
) -> RateLimitResult:
    """
    Check rate limit for a request.
    
    Args:
        identifier: User ID or IP
        endpoint: Endpoint name
        tier: User tier string
        
    Returns:
        RateLimitResult
    """
    tier_enum = RateLimitTier(tier.lower()) if tier.lower() in [t.value for t in RateLimitTier] else RateLimitTier.DEVELOPER
    return await rate_limiter.check(identifier, endpoint, tier_enum)


def get_tier_from_subscription(subscription_tier: str) -> RateLimitTier:
    """
    Map subscription tier to rate limit tier.
    
    Args:
        subscription_tier: Subscription tier string
        
    Returns:
        RateLimitTier
    """
    tier_map = {
        "developer": RateLimitTier.DEVELOPER,
        "free": RateLimitTier.DEVELOPER,  # Legacy alias
        "plus": RateLimitTier.PLUS,
        "pro": RateLimitTier.PLUS,  # Legacy alias
        "enterprise": RateLimitTier.ENTERPRISE,
    }
    return tier_map.get(subscription_tier.lower(), RateLimitTier.DEVELOPER)
