"""
Audit Logging Service - Phase 4.2 GTM

Comprehensive audit logging for billing operations.
"""

import logging
import json
from datetime import datetime
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, asdict
from enum import Enum
import uuid

from sqlalchemy import Column, String, Integer, DateTime, Text, Index
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func
from sqlalchemy import select

from .db import Base

logger = logging.getLogger(__name__)


class AuditAction(str, Enum):
    """Audit action types."""
    # Credit operations
    CREDIT_DEDUCT = "credit_deduct"
    CREDIT_ADD = "credit_add"
    CREDIT_REFUND = "credit_refund"
    CREDIT_EXPIRE = "credit_expire"
    CREDIT_ROLLOVER = "credit_rollover"
    
    # Subscription operations
    SUBSCRIPTION_CREATE = "subscription_create"
    SUBSCRIPTION_UPDATE = "subscription_update"
    SUBSCRIPTION_CANCEL = "subscription_cancel"
    SUBSCRIPTION_RENEW = "subscription_renew"
    
    # Invoice operations
    INVOICE_CREATE = "invoice_create"
    INVOICE_FINALIZE = "invoice_finalize"
    INVOICE_PAID = "invoice_paid"
    INVOICE_VOID = "invoice_void"
    
    # Payment operations
    PAYMENT_SUCCESS = "payment_success"
    PAYMENT_FAILED = "payment_failed"
    PAYMENT_REFUND = "payment_refund"
    
    # Webhook operations
    WEBHOOK_RECEIVED = "webhook_received"
    WEBHOOK_PROCESSED = "webhook_processed"
    WEBHOOK_FAILED = "webhook_failed"
    
    # Admin operations
    ADMIN_CREDIT_ADJUST = "admin_credit_adjust"
    ADMIN_TIER_CHANGE = "admin_tier_change"
    ADMIN_OVERRIDE = "admin_override"
    
    # Security events
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    UNAUTHORIZED_ACCESS = "unauthorized_access"
    SUSPICIOUS_ACTIVITY = "suspicious_activity"


class AuditLog(Base):
    """Audit log entry."""
    __tablename__ = "audit_logs"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    # Who
    user_id = Column(String(64), index=True)
    actor_id = Column(String(64), index=True)  # Who performed the action (could be admin)
    actor_type = Column(String(32))  # user, admin, system, webhook
    
    # What
    action = Column(String(64), nullable=False, index=True)
    resource_type = Column(String(64))  # credit, subscription, invoice, etc.
    resource_id = Column(String(64))
    
    # Details
    details = Column(JSONB)
    old_value = Column(JSONB)
    new_value = Column(JSONB)
    
    # Context
    ip_address = Column(String(45))
    user_agent = Column(String(512))
    request_id = Column(String(64), index=True)
    
    # Result
    success = Column(String(10), default="true")
    error_message = Column(Text)
    
    # Timestamp
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    
    __table_args__ = (
        Index('ix_audit_user_action', 'user_id', 'action'),
        Index('ix_audit_created_action', 'created_at', 'action'),
    )


@dataclass
class AuditEntry:
    """Audit entry for logging."""
    action: str
    user_id: Optional[str] = None
    actor_id: Optional[str] = None
    actor_type: str = "system"
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
    old_value: Optional[Dict[str, Any]] = None
    new_value: Optional[Dict[str, Any]] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    request_id: Optional[str] = None
    success: bool = True
    error_message: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


class AuditLogger:
    """
    Audit logging service for billing operations.
    
    Features:
    - Comprehensive action logging
    - Before/after value tracking
    - Actor tracking (user, admin, system)
    - Request context (IP, user agent)
    - Queryable audit trail
    """
    
    async def log(
        self,
        entry: AuditEntry,
        db: AsyncSession,
    ) -> AuditLog:
        """
        Log an audit entry.
        
        Args:
            entry: AuditEntry to log
            db: Database session
            
        Returns:
            Created AuditLog
        """
        audit_log = AuditLog(
            user_id=entry.user_id,
            actor_id=entry.actor_id or entry.user_id,
            actor_type=entry.actor_type,
            action=entry.action,
            resource_type=entry.resource_type,
            resource_id=entry.resource_id,
            details=entry.details,
            old_value=entry.old_value,
            new_value=entry.new_value,
            ip_address=entry.ip_address,
            user_agent=entry.user_agent,
            request_id=entry.request_id,
            success="true" if entry.success else "false",
            error_message=entry.error_message,
        )
        
        db.add(audit_log)
        await db.commit()
        
        # Also log to standard logger
        log_msg = (
            f"AUDIT: {entry.action} | "
            f"user={entry.user_id} | "
            f"actor={entry.actor_id}({entry.actor_type}) | "
            f"resource={entry.resource_type}:{entry.resource_id} | "
            f"success={entry.success}"
        )
        if entry.success:
            logger.info(log_msg)
        else:
            logger.warning(f"{log_msg} | error={entry.error_message}")
        
        return audit_log
    
    async def log_credit_operation(
        self,
        action: AuditAction,
        user_id: str,
        amount: int,
        balance_before: int,
        balance_after: int,
        reference_type: Optional[str] = None,
        reference_id: Optional[str] = None,
        db: AsyncSession = None,
        **context,
    ) -> AuditLog:
        """
        Log a credit operation.
        
        Args:
            action: Credit action type
            user_id: User ID
            amount: Credit amount
            balance_before: Balance before operation
            balance_after: Balance after operation
            reference_type: Reference type
            reference_id: Reference ID
            db: Database session
            **context: Additional context (ip_address, request_id, etc.)
        """
        entry = AuditEntry(
            action=action.value,
            user_id=user_id,
            actor_id=context.get("actor_id", user_id),
            actor_type=context.get("actor_type", "user"),
            resource_type="credit",
            resource_id=reference_id,
            details={
                "amount": amount,
                "reference_type": reference_type,
            },
            old_value={"balance": balance_before},
            new_value={"balance": balance_after},
            ip_address=context.get("ip_address"),
            user_agent=context.get("user_agent"),
            request_id=context.get("request_id"),
        )
        
        return await self.log(entry, db)
    
    async def log_subscription_change(
        self,
        action: AuditAction,
        user_id: str,
        subscription_id: str,
        old_tier: Optional[str] = None,
        new_tier: Optional[str] = None,
        db: AsyncSession = None,
        **context,
    ) -> AuditLog:
        """Log a subscription change."""
        entry = AuditEntry(
            action=action.value,
            user_id=user_id,
            actor_id=context.get("actor_id", user_id),
            actor_type=context.get("actor_type", "user"),
            resource_type="subscription",
            resource_id=subscription_id,
            old_value={"tier": old_tier} if old_tier else None,
            new_value={"tier": new_tier} if new_tier else None,
            ip_address=context.get("ip_address"),
            request_id=context.get("request_id"),
        )
        
        return await self.log(entry, db)
    
    async def log_payment(
        self,
        action: AuditAction,
        user_id: str,
        amount: int,
        currency: str = "usd",
        payment_id: Optional[str] = None,
        success: bool = True,
        error_message: Optional[str] = None,
        db: AsyncSession = None,
        **context,
    ) -> AuditLog:
        """Log a payment event."""
        entry = AuditEntry(
            action=action.value,
            user_id=user_id,
            actor_id=context.get("actor_id", "stripe"),
            actor_type="webhook",
            resource_type="payment",
            resource_id=payment_id,
            details={
                "amount": amount,
                "currency": currency,
            },
            success=success,
            error_message=error_message,
            ip_address=context.get("ip_address"),
            request_id=context.get("request_id"),
        )
        
        return await self.log(entry, db)
    
    async def log_security_event(
        self,
        action: AuditAction,
        user_id: Optional[str],
        details: Dict[str, Any],
        db: AsyncSession = None,
        **context,
    ) -> AuditLog:
        """Log a security event."""
        entry = AuditEntry(
            action=action.value,
            user_id=user_id,
            actor_id=context.get("actor_id"),
            actor_type="system",
            resource_type="security",
            details=details,
            success=False,
            ip_address=context.get("ip_address"),
            user_agent=context.get("user_agent"),
            request_id=context.get("request_id"),
        )
        
        return await self.log(entry, db)
    
    async def query_logs(
        self,
        user_id: Optional[str] = None,
        action: Optional[str] = None,
        resource_type: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        limit: int = 100,
        offset: int = 0,
        db: AsyncSession = None,
    ) -> List[AuditLog]:
        """
        Query audit logs.
        
        Args:
            user_id: Filter by user ID
            action: Filter by action
            resource_type: Filter by resource type
            start_date: Filter by start date
            end_date: Filter by end date
            limit: Max results
            offset: Offset for pagination
            db: Database session
            
        Returns:
            List of AuditLog entries
        """
        query = select(AuditLog)
        
        if user_id:
            query = query.where(AuditLog.user_id == user_id)
        if action:
            query = query.where(AuditLog.action == action)
        if resource_type:
            query = query.where(AuditLog.resource_type == resource_type)
        if start_date:
            query = query.where(AuditLog.created_at >= start_date)
        if end_date:
            query = query.where(AuditLog.created_at <= end_date)
        
        query = query.order_by(AuditLog.created_at.desc())
        query = query.limit(limit).offset(offset)
        
        result = await db.execute(query)
        return result.scalars().all()
    
    async def get_user_activity(
        self,
        user_id: str,
        days: int = 30,
        db: AsyncSession = None,
    ) -> Dict[str, Any]:
        """
        Get user activity summary.
        
        Args:
            user_id: User ID
            days: Number of days to analyze
            db: Database session
            
        Returns:
            Activity summary
        """
        from sqlalchemy import func as sql_func
        
        start_date = datetime.utcnow() - timedelta(days=days)
        
        # Count by action
        result = await db.execute(
            select(
                AuditLog.action,
                sql_func.count().label("count"),
            )
            .where(
                AuditLog.user_id == user_id,
                AuditLog.created_at >= start_date,
            )
            .group_by(AuditLog.action)
        )
        action_counts = {row.action: row.count for row in result.all()}
        
        # Get recent logs
        recent_result = await db.execute(
            select(AuditLog)
            .where(AuditLog.user_id == user_id)
            .order_by(AuditLog.created_at.desc())
            .limit(10)
        )
        recent_logs = recent_result.scalars().all()
        
        return {
            "user_id": user_id,
            "period_days": days,
            "action_counts": action_counts,
            "total_actions": sum(action_counts.values()),
            "recent_activity": [
                {
                    "action": log.action,
                    "resource_type": log.resource_type,
                    "created_at": log.created_at.isoformat() if log.created_at else None,
                    "success": log.success,
                }
                for log in recent_logs
            ],
        }


# Import timedelta
from datetime import timedelta

# Global instance
audit_logger = AuditLogger()


# ============================================
# CONVENIENCE FUNCTIONS
# ============================================

async def log_credit_deduction(
    user_id: str,
    amount: int,
    balance_before: int,
    balance_after: int,
    reference_type: str,
    db: AsyncSession,
    **context,
):
    """Log a credit deduction."""
    return await audit_logger.log_credit_operation(
        action=AuditAction.CREDIT_DEDUCT,
        user_id=user_id,
        amount=amount,
        balance_before=balance_before,
        balance_after=balance_after,
        reference_type=reference_type,
        db=db,
        **context,
    )


async def log_rate_limit_exceeded(
    user_id: str,
    endpoint: str,
    ip_address: str,
    db: AsyncSession,
):
    """Log rate limit exceeded event."""
    return await audit_logger.log_security_event(
        action=AuditAction.RATE_LIMIT_EXCEEDED,
        user_id=user_id,
        details={"endpoint": endpoint},
        db=db,
        ip_address=ip_address,
    )
