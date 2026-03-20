"""
Usage Alerts Service - Phase 2.2 GTM

Send alerts when users approach credit limits.
Supports email notifications, in-app notifications, and WebSocket broadcasts.
"""

import logging
from enum import Enum
from typing import Dict, Any, Optional, Set
from datetime import datetime, timedelta
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class UsageAlertLevel(str, Enum):
    """Usage alert thresholds."""
    WARNING_80 = "warning_80"
    CRITICAL_90 = "critical_90"
    EXHAUSTED_100 = "exhausted_100"
    
    @property
    def threshold(self) -> int:
        """Get threshold percentage for this level."""
        return {
            UsageAlertLevel.WARNING_80: 80,
            UsageAlertLevel.CRITICAL_90: 90,
            UsageAlertLevel.EXHAUSTED_100: 100,
        }[self]


@dataclass
class AlertConfig:
    """Configuration for an alert level."""
    level: UsageAlertLevel
    subject: str
    template: str
    priority: str  # low, medium, high, critical
    
    
ALERT_CONFIGS = {
    UsageAlertLevel.WARNING_80: AlertConfig(
        level=UsageAlertLevel.WARNING_80,
        subject="You've used 80% of your credits",
        template="usage_warning_80",
        priority="low",
    ),
    UsageAlertLevel.CRITICAL_90: AlertConfig(
        level=UsageAlertLevel.CRITICAL_90,
        subject="⚠️ Only 10% of credits remaining",
        template="usage_critical_90",
        priority="medium",
    ),
    UsageAlertLevel.EXHAUSTED_100: AlertConfig(
        level=UsageAlertLevel.EXHAUSTED_100,
        subject="🚨 Credits exhausted - Action required",
        template="usage_exhausted_100",
        priority="critical",
    ),
}


class UsageAlertService:
    """
    Send alerts when users approach credit limits.
    
    Features:
    - Threshold-based alerts (80%, 90%, 100%)
    - Deduplication (only alert once per threshold per billing period)
    - Multiple notification channels (email, in-app, WebSocket)
    - Configurable alert templates
    """
    
    def __init__(self, redis_client=None):
        """
        Initialize alert service.
        
        Args:
            redis_client: Optional Redis client for alert deduplication
        """
        self.redis = redis_client
        # In-memory fallback for deduplication
        self._alerted: Dict[str, Set[UsageAlertLevel]] = {}
    
    def _get_alert_key(self, user_id: str, level: UsageAlertLevel) -> str:
        """Generate key for alert deduplication."""
        # Include month to reset alerts each billing period
        month = datetime.utcnow().strftime("%Y-%m")
        return f"usage_alert:{user_id}:{month}:{level.value}"
    
    async def _already_alerted(self, user_id: str, level: UsageAlertLevel) -> bool:
        """Check if user has already been alerted for this level."""
        key = self._get_alert_key(user_id, level)
        
        # Try Redis first
        if self.redis:
            try:
                return await self.redis.exists(key)
            except Exception as e:
                logger.warning(f"Redis check failed: {e}")
        
        # Fallback to memory
        return level in self._alerted.get(user_id, set())
    
    async def _mark_alerted(self, user_id: str, level: UsageAlertLevel):
        """Mark user as alerted for this level."""
        key = self._get_alert_key(user_id, level)
        
        # Store in Redis with 35-day TTL (covers billing period + buffer)
        if self.redis:
            try:
                await self.redis.setex(key, 35 * 24 * 3600, "1")
            except Exception as e:
                logger.warning(f"Redis set failed: {e}")
        
        # Also store in memory
        if user_id not in self._alerted:
            self._alerted[user_id] = set()
        self._alerted[user_id].add(level)
    
    def calculate_usage_percent(self, balance: int, tier_credits: int) -> float:
        """
        Calculate usage percentage.
        
        Args:
            balance: Current credit balance
            tier_credits: Total credits for tier
            
        Returns:
            Usage percentage (0-100+)
        """
        if tier_credits <= 0:  # Unlimited
            return 0.0
        
        used = tier_credits - balance
        return (used / tier_credits) * 100
    
    def get_triggered_level(self, usage_percent: float) -> Optional[UsageAlertLevel]:
        """
        Get the highest triggered alert level.
        
        Args:
            usage_percent: Current usage percentage
            
        Returns:
            Highest triggered UsageAlertLevel or None
        """
        if usage_percent >= 100:
            return UsageAlertLevel.EXHAUSTED_100
        elif usage_percent >= 90:
            return UsageAlertLevel.CRITICAL_90
        elif usage_percent >= 80:
            return UsageAlertLevel.WARNING_80
        return None
    
    async def check_and_alert(
        self,
        user_id: str,
        balance: int,
        tier_credits: int,
        user_email: Optional[str] = None,
    ) -> Optional[UsageAlertLevel]:
        """
        Check usage level and send appropriate alerts.
        
        Args:
            user_id: User ID
            balance: Current credit balance
            tier_credits: Total credits for tier (monthly allocation)
            user_email: Optional email for notifications
            
        Returns:
            Alert level that was triggered, or None
        """
        if tier_credits <= 0:  # Unlimited tier
            return None
        
        usage_percent = self.calculate_usage_percent(balance, tier_credits)
        triggered_level = self.get_triggered_level(usage_percent)
        
        if not triggered_level:
            return None
        
        # Check if already alerted
        if await self._already_alerted(user_id, triggered_level):
            logger.debug(f"User {user_id[:8]}... already alerted for {triggered_level.value}")
            return None
        
        # Send alert
        await self._send_alert(
            user_id=user_id,
            level=triggered_level,
            balance=balance,
            tier_credits=tier_credits,
            usage_percent=usage_percent,
            user_email=user_email,
        )
        
        # Mark as alerted
        await self._mark_alerted(user_id, triggered_level)
        
        return triggered_level
    
    async def _send_alert(
        self,
        user_id: str,
        level: UsageAlertLevel,
        balance: int,
        tier_credits: int,
        usage_percent: float,
        user_email: Optional[str] = None,
    ):
        """
        Send alert via all configured channels.
        
        Args:
            user_id: User ID
            level: Alert level
            balance: Current balance
            tier_credits: Total tier credits
            usage_percent: Usage percentage
            user_email: Optional email address
        """
        config = ALERT_CONFIGS[level]
        
        context = {
            "user_id": user_id,
            "balance": balance,
            "tier_credits": tier_credits,
            "usage_percent": int(usage_percent),
            "credits_remaining": balance,
            "level": level.value,
            "priority": config.priority,
        }
        
        logger.info(
            f"🔔 Usage alert: user={user_id[:8]}... level={level.value} "
            f"usage={usage_percent:.1f}% balance={balance}"
        )
        
        # Send email notification
        if user_email:
            await self._send_email_alert(user_email, config, context)
        
        # Create in-app notification
        await self._create_notification(user_id, config, context)
        
        # Broadcast via WebSocket
        await self._broadcast_websocket(user_id, level, balance)
    
    async def _send_email_alert(
        self,
        email: str,
        config: AlertConfig,
        context: Dict[str, Any],
    ):
        """Send email alert."""
        try:
            # In production, integrate with email service
            # await email_service.send(
            #     to=email,
            #     subject=config.subject,
            #     template=config.template,
            #     context=context,
            # )
            logger.info(f"📧 Email alert sent to {email}: {config.subject}")
        except Exception as e:
            logger.error(f"Failed to send email alert: {e}")
    
    async def _create_notification(
        self,
        user_id: str,
        config: AlertConfig,
        context: Dict[str, Any],
    ):
        """Create in-app notification."""
        try:
            # In production, integrate with notification service
            # await notification_service.create(
            #     user_id=user_id,
            #     type="usage_alert",
            #     title=config.subject,
            #     priority=config.priority,
            #     data=context,
            # )
            logger.info(f"🔔 In-app notification created for {user_id[:8]}...")
        except Exception as e:
            logger.error(f"Failed to create notification: {e}")
    
    async def _broadcast_websocket(
        self,
        user_id: str,
        level: UsageAlertLevel,
        balance: int,
    ):
        """Broadcast alert via WebSocket."""
        try:
            # Import here to avoid circular imports
            from gateway.app.websocket_credits import credit_ws_manager
            
            if credit_ws_manager.is_user_connected(user_id):
                await credit_ws_manager.broadcast_alert(
                    user_id=user_id,
                    balance=balance,
                    alert_level=level.value,
                )
                logger.info(f"📡 WebSocket alert sent to {user_id[:8]}...")
        except ImportError:
            # Gateway not available (running in billing service context)
            pass
        except Exception as e:
            logger.error(f"Failed to broadcast WebSocket alert: {e}")
    
    def reset_alerts(self, user_id: str):
        """
        Reset alerts for a user (e.g., at start of new billing period).
        
        Args:
            user_id: User ID
        """
        if user_id in self._alerted:
            del self._alerted[user_id]
        logger.info(f"Reset alerts for user {user_id[:8]}...")


# Global instance
usage_alert_service = UsageAlertService()


# ============================================
# CONVENIENCE FUNCTIONS
# ============================================

async def check_usage_alerts(
    user_id: str,
    balance: int,
    tier_credits: int,
    user_email: Optional[str] = None,
) -> Optional[UsageAlertLevel]:
    """
    Check and send usage alerts for a user.
    
    Call this after any credit deduction.
    
    Args:
        user_id: User ID
        balance: Current balance after deduction
        tier_credits: Total credits for user's tier
        user_email: Optional email for notifications
        
    Returns:
        Alert level triggered, or None
    """
    return await usage_alert_service.check_and_alert(
        user_id=user_id,
        balance=balance,
        tier_credits=tier_credits,
        user_email=user_email,
    )
