"""
Tests for Rate Limiter Service - Phase 4.1 GTM

Tests rate limiting functionality.
"""

import pytest
import time
from unittest.mock import AsyncMock, MagicMock, patch

from app.rate_limiter import (
    RateLimiter,
    RateLimitResult,
    RateLimitTier,
    RATE_LIMITS,
    rate_limiter,
    check_rate_limit,
    get_tier_from_subscription,
)


class TestRateLimitTier:
    """Test RateLimitTier enum."""
    
    def test_tier_values(self):
        """Test all tier values."""
        assert RateLimitTier.FREE.value == "free"
        assert RateLimitTier.PLUS.value == "plus"
        assert RateLimitTier.ENTERPRISE.value == "enterprise"
        assert RateLimitTier.INTERNAL.value == "internal"


class TestRateLimits:
    """Test rate limit configuration."""
    
    def test_free_tier_limits(self):
        """Test free tier limits."""
        limits = RATE_LIMITS[RateLimitTier.FREE]
        assert limits["default"] == 60
        assert limits["credit_deduct"] == 100
        assert limits["credit_check"] == 200
    
    def test_plus_tier_limits(self):
        """Test plus tier limits."""
        limits = RATE_LIMITS[RateLimitTier.PLUS]
        assert limits["default"] == 300
        assert limits["credit_deduct"] == 500
    
    def test_enterprise_tier_limits(self):
        """Test enterprise tier limits."""
        limits = RATE_LIMITS[RateLimitTier.ENTERPRISE]
        assert limits["default"] == 1000
        assert limits["credit_deduct"] == 2000
    
    def test_tier_hierarchy(self):
        """Test tiers have increasing limits."""
        free = RATE_LIMITS[RateLimitTier.FREE]["default"]
        plus = RATE_LIMITS[RateLimitTier.PLUS]["default"]
        enterprise = RATE_LIMITS[RateLimitTier.ENTERPRISE]["default"]
        
        assert free < plus < enterprise


class TestRateLimitResult:
    """Test RateLimitResult dataclass."""
    
    def test_to_headers_allowed(self):
        """Test headers for allowed request."""
        result = RateLimitResult(
            allowed=True,
            remaining=50,
            limit=60,
            reset_at=1234567890,
        )
        
        headers = result.to_headers()
        
        assert headers["X-RateLimit-Limit"] == "60"
        assert headers["X-RateLimit-Remaining"] == "50"
        assert headers["X-RateLimit-Reset"] == "1234567890"
        assert "Retry-After" not in headers
    
    def test_to_headers_blocked(self):
        """Test headers for blocked request."""
        result = RateLimitResult(
            allowed=False,
            remaining=0,
            limit=60,
            reset_at=1234567890,
            retry_after=30,
        )
        
        headers = result.to_headers()
        
        assert headers["X-RateLimit-Remaining"] == "0"
        assert headers["Retry-After"] == "30"


class TestRateLimiter:
    """Test RateLimiter class."""
    
    def setup_method(self):
        self.limiter = RateLimiter(window_seconds=60)
    
    def test_get_limit_default(self):
        """Test getting default limit."""
        limit = self.limiter._get_limit(RateLimitTier.FREE, "unknown_endpoint")
        assert limit == 60  # Default for free tier
    
    def test_get_limit_specific(self):
        """Test getting specific endpoint limit."""
        limit = self.limiter._get_limit(RateLimitTier.FREE, "credit_deduct")
        assert limit == 100
    
    @pytest.mark.asyncio
    async def test_check_allows_first_request(self):
        """Test first request is allowed."""
        result = await self.limiter.check("user123", "default", RateLimitTier.FREE)
        
        assert result.allowed is True
        assert result.remaining == 59  # 60 - 1
        assert result.limit == 60
    
    @pytest.mark.asyncio
    async def test_check_tracks_requests(self):
        """Test requests are tracked."""
        # Make several requests
        for i in range(5):
            result = await self.limiter.check("user456", "default", RateLimitTier.FREE)
        
        assert result.allowed is True
        assert result.remaining == 55  # 60 - 5
    
    @pytest.mark.asyncio
    async def test_check_blocks_over_limit(self):
        """Test requests over limit are blocked."""
        # Exhaust the limit
        for i in range(60):
            await self.limiter.check("user789", "default", RateLimitTier.FREE)
        
        # Next request should be blocked
        result = await self.limiter.check("user789", "default", RateLimitTier.FREE)
        
        assert result.allowed is False
        assert result.remaining == 0
        assert result.retry_after is not None
        assert result.retry_after > 0
    
    @pytest.mark.asyncio
    async def test_check_different_users_independent(self):
        """Test different users have independent limits."""
        # Exhaust one user's limit
        for i in range(60):
            await self.limiter.check("userA", "default", RateLimitTier.FREE)
        
        # Other user should still be allowed
        result = await self.limiter.check("userB", "default", RateLimitTier.FREE)
        
        assert result.allowed is True
    
    @pytest.mark.asyncio
    async def test_check_different_endpoints_independent(self):
        """Test different endpoints have independent limits."""
        # Use up default endpoint
        for i in range(60):
            await self.limiter.check("userC", "default", RateLimitTier.FREE)
        
        # credit_check endpoint should still be allowed
        result = await self.limiter.check("userC", "credit_check", RateLimitTier.FREE)
        
        assert result.allowed is True
    
    @pytest.mark.asyncio
    async def test_reset_clears_limit(self):
        """Test reset clears rate limit."""
        # Make some requests
        for i in range(30):
            await self.limiter.check("userD", "default", RateLimitTier.FREE)
        
        # Reset
        await self.limiter.reset("userD", "default")
        
        # Should have full limit again
        result = await self.limiter.check("userD", "default", RateLimitTier.FREE)
        assert result.remaining == 59
    
    @pytest.mark.asyncio
    async def test_get_status(self):
        """Test get_status returns current state."""
        # Make some requests
        for i in range(10):
            await self.limiter.check("userE", "default", RateLimitTier.FREE)
        
        status = await self.limiter.get_status("userE", "default", RateLimitTier.FREE)
        
        assert status["identifier"] == "userE"
        assert status["endpoint"] == "default"
        assert status["tier"] == "free"
        assert status["limit"] == 60
        assert status["used"] == 10
        assert status["remaining"] == 50


class TestConvenienceFunctions:
    """Test convenience functions."""
    
    @pytest.mark.asyncio
    async def test_check_rate_limit(self):
        """Test check_rate_limit function."""
        result = await check_rate_limit("user123", "default", "free")
        
        assert isinstance(result, RateLimitResult)
        assert result.allowed is True
    
    def test_get_tier_from_subscription(self):
        """Test tier mapping from subscription."""
        assert get_tier_from_subscription("developer") == RateLimitTier.FREE
        assert get_tier_from_subscription("free") == RateLimitTier.FREE
        assert get_tier_from_subscription("plus") == RateLimitTier.PLUS
        assert get_tier_from_subscription("pro") == RateLimitTier.PLUS
        assert get_tier_from_subscription("enterprise") == RateLimitTier.ENTERPRISE
        assert get_tier_from_subscription("unknown") == RateLimitTier.FREE


class TestGlobalInstance:
    """Test global instance."""
    
    def test_global_instance_exists(self):
        """Test global instance exists."""
        assert rate_limiter is not None
        assert isinstance(rate_limiter, RateLimiter)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
