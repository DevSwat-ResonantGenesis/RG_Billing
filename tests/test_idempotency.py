"""
Tests for Billing Idempotency Service - Phase 1.3 GTM

Tests idempotency key generation, duplicate detection, and caching.
"""

import pytest
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from app.idempotency import (
    BillingIdempotency,
    IdempotencyResult,
    IdempotencyRecord,
    get_idempotency,
    init_idempotency,
)


class TestIdempotencyKeyGeneration:
    """Test idempotency key generation."""
    
    def setup_method(self):
        self.idempotency = BillingIdempotency()
    
    def test_generate_key_basic(self):
        """Test basic key generation."""
        key = self.idempotency.generate_key(
            user_id="user123",
            operation="credit_deduct",
            amount=100,
        )
        
        assert key is not None
        assert len(key) == 32  # SHA256 truncated to 32 chars
        assert isinstance(key, str)
    
    def test_generate_key_deterministic_same_minute(self):
        """Test same inputs in same minute produce same key."""
        key1 = self.idempotency.generate_key("user123", "credit_deduct", 100, "ref1")
        key2 = self.idempotency.generate_key("user123", "credit_deduct", 100, "ref1")
        
        # Same inputs in same minute should produce same key
        assert key1 == key2
    
    def test_generate_key_different_users(self):
        """Test different users produce different keys."""
        key1 = self.idempotency.generate_key("user1", "credit_deduct", 100)
        key2 = self.idempotency.generate_key("user2", "credit_deduct", 100)
        
        assert key1 != key2
    
    def test_generate_key_different_operations(self):
        """Test different operations produce different keys."""
        key1 = self.idempotency.generate_key("user1", "credit_deduct", 100)
        key2 = self.idempotency.generate_key("user1", "credit_add", 100)
        
        assert key1 != key2
    
    def test_generate_key_different_amounts(self):
        """Test different amounts produce different keys."""
        key1 = self.idempotency.generate_key("user1", "credit_deduct", 100)
        key2 = self.idempotency.generate_key("user1", "credit_deduct", 200)
        
        assert key1 != key2
    
    def test_generate_key_strict_no_time_bucket(self):
        """Test strict key generation without time bucket."""
        key1 = self.idempotency.generate_key_strict("user1", "credit_deduct", 100, "ref123")
        key2 = self.idempotency.generate_key_strict("user1", "credit_deduct", 100, "ref123")
        
        # Strict keys should always be the same for same inputs
        assert key1 == key2
        assert len(key1) == 32
    
    def test_generate_key_with_reference_id(self):
        """Test key generation with reference ID."""
        key1 = self.idempotency.generate_key("user1", "op", 100, "ref1")
        key2 = self.idempotency.generate_key("user1", "op", 100, "ref2")
        
        assert key1 != key2
    
    def test_generate_key_with_extra(self):
        """Test key generation with extra context."""
        key1 = self.idempotency.generate_key("user1", "op", 100, extra="context1")
        key2 = self.idempotency.generate_key("user1", "op", 100, extra="context2")
        
        assert key1 != key2


class TestIdempotencyMemoryCache:
    """Test in-memory idempotency cache."""
    
    def setup_method(self):
        self.idempotency = BillingIdempotency()
    
    @pytest.mark.asyncio
    async def test_check_not_duplicate(self):
        """Test check returns not duplicate for new key."""
        result = await self.idempotency.check("new_key_123")
        
        assert isinstance(result, IdempotencyResult)
        assert result.is_duplicate is False
        assert result.previous_result is None
        assert result.idempotency_key == "new_key_123"
    
    @pytest.mark.asyncio
    async def test_store_and_check_duplicate(self):
        """Test storing and checking for duplicate."""
        key = "test_key_456"
        result_data = {"balance": 100, "transaction_id": "tx123"}
        
        # Store result
        await self.idempotency.store(key, result_data)
        
        # Check should return duplicate
        check_result = await self.idempotency.check(key)
        
        assert check_result.is_duplicate is True
        assert check_result.previous_result == result_data
    
    @pytest.mark.asyncio
    async def test_check_and_store_atomic(self):
        """Test atomic check-and-store operation."""
        key = "atomic_key_789"
        result_data = {"success": True}
        
        # First call - not duplicate
        is_dup, prev = await self.idempotency.check_and_store(key, result_data)
        assert is_dup is False
        assert prev is None
        
        # Second call - duplicate
        is_dup, prev = await self.idempotency.check_and_store(key, {"new": "data"})
        assert is_dup is True
        assert prev == result_data
    
    @pytest.mark.asyncio
    async def test_expired_entry_not_duplicate(self):
        """Test expired entries are not considered duplicates."""
        key = "expired_key"
        
        # Manually add expired entry to memory cache
        expired_time = datetime.utcnow() - timedelta(hours=1)
        self.idempotency._memory_cache[key] = ({"old": "data"}, expired_time)
        
        # Check should return not duplicate
        result = await self.idempotency.check(key)
        assert result.is_duplicate is False
    
    @pytest.mark.asyncio
    async def test_memory_cache_cleanup(self):
        """Test memory cache cleanup when too large."""
        # Add many entries
        for i in range(15000):
            self.idempotency._memory_cache[f"key_{i}"] = (
                {"data": i},
                datetime.utcnow() + timedelta(hours=1)
            )
        
        # Trigger cleanup via store
        await self.idempotency.store("trigger_cleanup", {"test": True})
        
        # Cache should be reduced
        assert len(self.idempotency._memory_cache) <= 10001


class TestIdempotencyResult:
    """Test IdempotencyResult dataclass."""
    
    def test_result_not_duplicate(self):
        """Test result for non-duplicate."""
        result = IdempotencyResult(
            is_duplicate=False,
            previous_result=None,
            idempotency_key="key123",
        )
        
        assert result.is_duplicate is False
        assert result.previous_result is None
        assert result.idempotency_key == "key123"
    
    def test_result_duplicate(self):
        """Test result for duplicate."""
        prev = {"balance": 500}
        result = IdempotencyResult(
            is_duplicate=True,
            previous_result=prev,
            idempotency_key="key456",
        )
        
        assert result.is_duplicate is True
        assert result.previous_result == prev


class TestGlobalIdempotency:
    """Test global idempotency instance functions."""
    
    def test_get_idempotency_returns_instance(self):
        """Test get_idempotency returns an instance."""
        instance = get_idempotency()
        assert isinstance(instance, BillingIdempotency)
    
    def test_get_idempotency_singleton(self):
        """Test get_idempotency returns same instance."""
        instance1 = get_idempotency()
        instance2 = get_idempotency()
        assert instance1 is instance2
    
    def test_init_idempotency(self):
        """Test init_idempotency creates new instance."""
        instance = init_idempotency()
        assert isinstance(instance, BillingIdempotency)


class TestIdempotencyWithMockedRedis:
    """Test idempotency with mocked Redis."""
    
    @pytest.mark.asyncio
    async def test_redis_check_hit(self):
        """Test Redis cache hit."""
        mock_redis = AsyncMock()
        mock_redis.get.return_value = '{"balance": 100}'
        
        idempotency = BillingIdempotency(redis_client=mock_redis)
        
        with patch('app.idempotency.REDIS_AVAILABLE', True):
            result = await idempotency.check("redis_key")
        
        assert result.is_duplicate is True
        assert result.previous_result == {"balance": 100}
    
    @pytest.mark.asyncio
    async def test_redis_check_miss(self):
        """Test Redis cache miss."""
        mock_redis = AsyncMock()
        mock_redis.get.return_value = None
        
        idempotency = BillingIdempotency(redis_client=mock_redis)
        
        with patch('app.idempotency.REDIS_AVAILABLE', True):
            result = await idempotency.check("missing_key")
        
        assert result.is_duplicate is False
    
    @pytest.mark.asyncio
    async def test_redis_store(self):
        """Test storing in Redis."""
        mock_redis = AsyncMock()
        
        idempotency = BillingIdempotency(redis_client=mock_redis)
        
        with patch('app.idempotency.REDIS_AVAILABLE', True):
            await idempotency.store("store_key", {"data": "value"})
        
        mock_redis.setex.assert_called_once()


class TestIdempotencyTTL:
    """Test TTL behavior."""
    
    def test_default_ttl(self):
        """Test default TTL is 24 hours."""
        idempotency = BillingIdempotency()
        assert idempotency.ttl == timedelta(hours=24)
    
    def test_custom_ttl(self):
        """Test custom TTL."""
        custom_ttl = timedelta(hours=1)
        idempotency = BillingIdempotency(ttl=custom_ttl)
        assert idempotency.ttl == custom_ttl


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
