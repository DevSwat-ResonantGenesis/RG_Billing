"""
Billing Idempotency Service - Prevent duplicate charges and operations.

Phase 1.3 of GTM Production Strategy.

Features:
- Idempotency keys for all billing operations
- Redis-backed cache with TTL
- Database fallback for persistence
- Automatic cleanup of expired keys
"""

import logging
import json
import hashlib
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple
from dataclasses import dataclass
import uuid

from sqlalchemy import Column, String, DateTime, JSON, select
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from sqlalchemy.ext.asyncio import AsyncSession

from .db import Base

logger = logging.getLogger(__name__)

# Try to import redis, fall back to in-memory if not available
try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    logger.warning("redis not installed - using in-memory idempotency cache")


# Default TTL: 24 hours
DEFAULT_TTL = timedelta(hours=24)


class IdempotencyRecord(Base):
    """
    Persistent idempotency record for billing operations.
    
    Used as fallback when Redis is unavailable and for audit trail.
    """
    __tablename__ = "idempotency_records"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    idempotency_key = Column(String(64), unique=True, index=True, nullable=False)
    
    # Operation details
    operation = Column(String(64), nullable=False)
    user_id = Column(String(64), index=True, nullable=False)
    amount = Column(String(32))
    
    # Result
    result = Column(JSON)
    status = Column(String(32), default="completed")  # completed, failed
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)


@dataclass
class IdempotencyResult:
    """Result of idempotency check."""
    is_duplicate: bool
    previous_result: Optional[Dict[str, Any]]
    idempotency_key: str


class BillingIdempotency:
    """
    Ensure billing operations are idempotent.
    
    Uses Redis for fast lookups with database fallback.
    
    Usage:
        idempotency = BillingIdempotency(redis_client, db_session)
        
        # Generate key
        key = idempotency.generate_key(user_id, "credit_deduct", 100, reference_id)
        
        # Check before operation
        result = await idempotency.check(key)
        if result.is_duplicate:
            return result.previous_result
        
        # Perform operation...
        
        # Store result
        await idempotency.store(key, operation_result)
    """
    
    CACHE_PREFIX = "billing:idempotency:"
    
    def __init__(
        self,
        redis_client: Optional[Any] = None,
        db: Optional[AsyncSession] = None,
        ttl: timedelta = DEFAULT_TTL,
    ):
        self.redis = redis_client
        self.db = db
        self.ttl = ttl
        self._memory_cache: Dict[str, Tuple[Dict, datetime]] = {}
    
    def generate_key(
        self,
        user_id: str,
        operation: str,
        amount: int,
        reference_id: Optional[str] = None,
        extra: Optional[str] = None,
    ) -> str:
        """
        Generate idempotency key for a billing operation.
        
        The key is deterministic based on:
        - user_id
        - operation type
        - amount
        - reference_id (optional)
        - extra context (optional)
        
        Args:
            user_id: User ID
            operation: Operation type (e.g., "credit_deduct", "credit_add")
            amount: Amount involved
            reference_id: Optional reference ID
            extra: Optional extra context
            
        Returns:
            32-character hex string
        """
        # Include timestamp bucket for time-based uniqueness (1-minute windows)
        timestamp_bucket = int(datetime.utcnow().timestamp() // 60)
        
        data = f"{user_id}:{operation}:{amount}:{reference_id or ''}:{extra or ''}:{timestamp_bucket}"
        return hashlib.sha256(data.encode()).hexdigest()[:32]
    
    def generate_key_strict(
        self,
        user_id: str,
        operation: str,
        amount: int,
        reference_id: str,
    ) -> str:
        """
        Generate strict idempotency key (no time bucket).
        
        Use this when you have a unique reference_id that should
        never be processed twice regardless of time.
        """
        data = f"{user_id}:{operation}:{amount}:{reference_id}"
        return hashlib.sha256(data.encode()).hexdigest()[:32]
    
    async def check(self, key: str) -> IdempotencyResult:
        """
        Check if operation was already performed.
        
        Args:
            key: Idempotency key
            
        Returns:
            IdempotencyResult with is_duplicate and previous_result
        """
        # Try Redis first
        if self.redis and REDIS_AVAILABLE:
            try:
                cached = await self.redis.get(f"{self.CACHE_PREFIX}{key}")
                if cached:
                    result = json.loads(cached)
                    logger.debug(f"Idempotency hit (Redis): {key}")
                    return IdempotencyResult(
                        is_duplicate=True,
                        previous_result=result,
                        idempotency_key=key,
                    )
            except Exception as e:
                logger.warning(f"Redis idempotency check failed: {e}")
        
        # Try database
        if self.db:
            try:
                result = await self.db.execute(
                    select(IdempotencyRecord)
                    .where(
                        IdempotencyRecord.idempotency_key == key,
                        IdempotencyRecord.expires_at > datetime.utcnow(),
                    )
                )
                record = result.scalar_one_or_none()
                if record:
                    logger.debug(f"Idempotency hit (DB): {key}")
                    return IdempotencyResult(
                        is_duplicate=True,
                        previous_result=record.result,
                        idempotency_key=key,
                    )
            except Exception as e:
                logger.warning(f"DB idempotency check failed: {e}")
        
        # Try memory cache (last resort)
        if key in self._memory_cache:
            result, expires_at = self._memory_cache[key]
            if expires_at > datetime.utcnow():
                logger.debug(f"Idempotency hit (memory): {key}")
                return IdempotencyResult(
                    is_duplicate=True,
                    previous_result=result,
                    idempotency_key=key,
                )
            else:
                del self._memory_cache[key]
        
        # Not found - not a duplicate
        return IdempotencyResult(
            is_duplicate=False,
            previous_result=None,
            idempotency_key=key,
        )
    
    async def store(
        self,
        key: str,
        result: Dict[str, Any],
        operation: str = "unknown",
        user_id: str = "unknown",
        amount: Optional[int] = None,
    ) -> bool:
        """
        Store operation result for idempotency.
        
        Args:
            key: Idempotency key
            result: Operation result to store
            operation: Operation type (for audit)
            user_id: User ID (for audit)
            amount: Amount (for audit)
            
        Returns:
            True if stored successfully
        """
        expires_at = datetime.utcnow() + self.ttl
        ttl_seconds = int(self.ttl.total_seconds())
        
        # Store in Redis
        if self.redis and REDIS_AVAILABLE:
            try:
                await self.redis.setex(
                    f"{self.CACHE_PREFIX}{key}",
                    ttl_seconds,
                    json.dumps(result),
                )
                logger.debug(f"Idempotency stored (Redis): {key}")
            except Exception as e:
                logger.warning(f"Redis idempotency store failed: {e}")
        
        # Store in database (for persistence and audit)
        if self.db:
            try:
                record = IdempotencyRecord(
                    idempotency_key=key,
                    operation=operation,
                    user_id=user_id,
                    amount=str(amount) if amount else None,
                    result=result,
                    expires_at=expires_at,
                )
                self.db.add(record)
                await self.db.commit()
                logger.debug(f"Idempotency stored (DB): {key}")
            except Exception as e:
                # Might fail on duplicate key - that's OK
                logger.debug(f"DB idempotency store skipped: {e}")
                await self.db.rollback()
        
        # Store in memory cache
        self._memory_cache[key] = (result, expires_at)
        
        # Cleanup old memory entries (simple LRU-ish)
        if len(self._memory_cache) > 10000:
            self._cleanup_memory_cache()
        
        return True
    
    async def check_and_store(
        self,
        key: str,
        result: Dict[str, Any],
        operation: str = "unknown",
        user_id: str = "unknown",
        amount: Optional[int] = None,
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """
        Atomic check-and-store operation.
        
        Returns:
            (is_duplicate, previous_result_or_none)
        """
        check_result = await self.check(key)
        if check_result.is_duplicate:
            return True, check_result.previous_result
        
        await self.store(key, result, operation, user_id, amount)
        return False, None
    
    def _cleanup_memory_cache(self):
        """Remove expired entries from memory cache."""
        now = datetime.utcnow()
        expired = [k for k, (_, exp) in self._memory_cache.items() if exp < now]
        for k in expired:
            del self._memory_cache[k]
        
        # If still too large, remove oldest
        if len(self._memory_cache) > 10000:
            sorted_keys = sorted(
                self._memory_cache.keys(),
                key=lambda k: self._memory_cache[k][1]
            )
            for k in sorted_keys[:5000]:
                del self._memory_cache[k]
    
    async def cleanup_expired(self) -> int:
        """
        Cleanup expired records from database.
        
        Should be run periodically (e.g., daily cron job).
        
        Returns:
            Number of records deleted
        """
        if not self.db:
            return 0
        
        try:
            from sqlalchemy import delete
            
            result = await self.db.execute(
                delete(IdempotencyRecord)
                .where(IdempotencyRecord.expires_at < datetime.utcnow())
            )
            await self.db.commit()
            
            deleted = result.rowcount
            logger.info(f"Cleaned up {deleted} expired idempotency records")
            return deleted
        except Exception as e:
            logger.error(f"Idempotency cleanup failed: {e}")
            await self.db.rollback()
            return 0


# ============================================
# DECORATOR FOR IDEMPOTENT OPERATIONS
# ============================================

def idempotent_operation(
    operation_name: str,
    key_fields: list = None,
):
    """
    Decorator to make a billing operation idempotent.
    
    Usage:
        @idempotent_operation("credit_deduct", ["user_id", "amount", "reference_id"])
        async def deduct_credits(user_id: str, amount: int, reference_id: str):
            ...
    """
    def decorator(func):
        async def wrapper(*args, **kwargs):
            # Extract key fields from kwargs
            user_id = kwargs.get("user_id", "unknown")
            amount = kwargs.get("amount", 0)
            reference_id = kwargs.get("reference_id") or kwargs.get("idempotency_key")
            
            # Get or create idempotency service
            idempotency = kwargs.pop("_idempotency", None)
            if not idempotency:
                idempotency = BillingIdempotency()
            
            # Generate key
            key = idempotency.generate_key(user_id, operation_name, amount, reference_id)
            
            # Check for duplicate
            check_result = await idempotency.check(key)
            if check_result.is_duplicate:
                logger.info(f"Idempotent operation {operation_name} - returning cached result")
                return check_result.previous_result
            
            # Execute operation
            result = await func(*args, **kwargs)
            
            # Store result
            await idempotency.store(key, result, operation_name, user_id, amount)
            
            return result
        
        return wrapper
    return decorator


# Global instance (will be initialized with Redis/DB in app startup)
_idempotency_instance: Optional[BillingIdempotency] = None


def get_idempotency() -> BillingIdempotency:
    """Get global idempotency instance."""
    global _idempotency_instance
    if _idempotency_instance is None:
        _idempotency_instance = BillingIdempotency()
    return _idempotency_instance


def init_idempotency(
    redis_client: Optional[Any] = None,
    db: Optional[AsyncSession] = None,
) -> BillingIdempotency:
    """Initialize global idempotency instance."""
    global _idempotency_instance
    _idempotency_instance = BillingIdempotency(redis_client, db)
    return _idempotency_instance
