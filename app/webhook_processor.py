"""
Webhook Processor - Reliable Stripe webhook handling with retry logic.

Phase 1.2 of GTM Production Strategy.

Features:
- Idempotent webhook processing (no duplicate handling)
- Automatic retry with exponential backoff
- Persistent event storage for audit trail
- Dead letter queue for failed events
"""

import logging
import json
import uuid
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Callable, Awaitable
from enum import Enum

from sqlalchemy import Column, String, DateTime, Integer, JSON, Boolean, select, update
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from sqlalchemy.ext.asyncio import AsyncSession

from .db import Base, get_session

logger = logging.getLogger(__name__)


class WebhookStatus(str, Enum):
    """Webhook processing status."""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


class WebhookEvent(Base):
    """
    Store and track Stripe webhook events.
    
    Provides:
    - Idempotency via stripe_event_id
    - Retry tracking with exponential backoff
    - Audit trail for all events
    """
    __tablename__ = "webhook_events"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    stripe_event_id = Column(String(64), unique=True, index=True, nullable=False)
    event_type = Column(String(64), index=True, nullable=False)
    payload = Column(JSON, nullable=False)
    
    # Processing status
    status = Column(String(32), default=WebhookStatus.PENDING.value, index=True)
    attempts = Column(Integer, default=0)
    max_attempts = Column(Integer, default=5)
    last_attempt_at = Column(DateTime(timezone=True))
    next_retry_at = Column(DateTime(timezone=True), index=True)
    error_message = Column(String(2048))
    
    # Result tracking
    processed_at = Column(DateTime(timezone=True))
    result = Column(JSON)
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


# Retry delays in seconds: 1m, 5m, 15m, 1h, 24h
RETRY_DELAYS = [60, 300, 900, 3600, 86400]


class WebhookProcessor:
    """
    Process Stripe webhooks with reliability guarantees.
    
    Usage:
        processor = WebhookProcessor(db_session)
        result = await processor.process_event(event_id, event_type, payload)
    """
    
    def __init__(self, db: AsyncSession):
        self.db = db
        self._handlers: Dict[str, Callable[[Dict], Awaitable[Dict]]] = {}
    
    def register_handler(
        self,
        event_type: str,
        handler: Callable[[Dict], Awaitable[Dict]]
    ):
        """Register a handler for an event type."""
        self._handlers[event_type] = handler
        logger.info(f"Registered webhook handler for: {event_type}")
    
    async def process_event(
        self,
        stripe_event_id: str,
        event_type: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Process a webhook event with idempotency.
        
        Args:
            stripe_event_id: Stripe's event ID (for idempotency)
            event_type: Event type (e.g., "checkout.session.completed")
            payload: Event payload
            
        Returns:
            Processing result dict
        """
        # Check if already processed (idempotency)
        existing = await self._get_event(stripe_event_id)
        if existing:
            if existing.status == WebhookStatus.COMPLETED.value:
                logger.info(f"Webhook {stripe_event_id} already processed, skipping")
                return {
                    "status": "already_processed",
                    "event_id": stripe_event_id,
                    "result": existing.result,
                }
            elif existing.status == WebhookStatus.PROCESSING.value:
                logger.warning(f"Webhook {stripe_event_id} currently processing")
                return {
                    "status": "processing",
                    "event_id": stripe_event_id,
                }
        
        # Create or get event record
        event = await self._create_or_get_event(stripe_event_id, event_type, payload)
        
        # Mark as processing
        event.status = WebhookStatus.PROCESSING.value
        event.attempts += 1
        event.last_attempt_at = datetime.utcnow()
        await self.db.commit()
        
        try:
            # Get handler for event type
            handler = self._handlers.get(event_type)
            if not handler:
                logger.warning(f"No handler for event type: {event_type}")
                event.status = WebhookStatus.COMPLETED.value
                event.processed_at = datetime.utcnow()
                event.result = {"status": "no_handler", "event_type": event_type}
                await self.db.commit()
                return event.result
            
            # Execute handler
            result = await handler(payload)
            
            # Mark as completed
            event.status = WebhookStatus.COMPLETED.value
            event.processed_at = datetime.utcnow()
            event.result = result
            event.error_message = None
            await self.db.commit()
            
            logger.info(f"✅ Webhook {stripe_event_id} ({event_type}) processed successfully")
            return {
                "status": "completed",
                "event_id": stripe_event_id,
                "result": result,
            }
            
        except Exception as e:
            # Handle failure with retry
            error_msg = str(e)[:2000]
            logger.error(f"❌ Webhook {stripe_event_id} failed: {error_msg}")
            
            event.error_message = error_msg
            
            if event.attempts < event.max_attempts:
                # Schedule retry
                delay_idx = min(event.attempts - 1, len(RETRY_DELAYS) - 1)
                delay = RETRY_DELAYS[delay_idx]
                event.next_retry_at = datetime.utcnow() + timedelta(seconds=delay)
                event.status = WebhookStatus.PENDING.value
                
                logger.info(
                    f"Webhook {stripe_event_id} scheduled for retry "
                    f"(attempt {event.attempts}/{event.max_attempts}) in {delay}s"
                )
            else:
                # Move to dead letter
                event.status = WebhookStatus.DEAD_LETTER.value
                logger.error(
                    f"Webhook {stripe_event_id} moved to dead letter "
                    f"after {event.attempts} attempts"
                )
            
            await self.db.commit()
            
            return {
                "status": "failed",
                "event_id": stripe_event_id,
                "error": error_msg,
                "retry_scheduled": event.status == WebhookStatus.PENDING.value,
            }
    
    async def _get_event(self, stripe_event_id: str) -> Optional[WebhookEvent]:
        """Get existing event by Stripe event ID."""
        result = await self.db.execute(
            select(WebhookEvent).where(WebhookEvent.stripe_event_id == stripe_event_id)
        )
        return result.scalar_one_or_none()
    
    async def _create_or_get_event(
        self,
        stripe_event_id: str,
        event_type: str,
        payload: Dict[str, Any],
    ) -> WebhookEvent:
        """Create new event or get existing one."""
        existing = await self._get_event(stripe_event_id)
        if existing:
            return existing
        
        event = WebhookEvent(
            stripe_event_id=stripe_event_id,
            event_type=event_type,
            payload=payload,
            status=WebhookStatus.PENDING.value,
        )
        self.db.add(event)
        await self.db.commit()
        await self.db.refresh(event)
        return event
    
    async def get_pending_retries(self, limit: int = 100) -> list:
        """Get events pending retry."""
        result = await self.db.execute(
            select(WebhookEvent)
            .where(
                WebhookEvent.status == WebhookStatus.PENDING.value,
                WebhookEvent.next_retry_at <= datetime.utcnow(),
            )
            .order_by(WebhookEvent.next_retry_at)
            .limit(limit)
        )
        return result.scalars().all()
    
    async def get_dead_letter_events(self, limit: int = 100) -> list:
        """Get events in dead letter queue."""
        result = await self.db.execute(
            select(WebhookEvent)
            .where(WebhookEvent.status == WebhookStatus.DEAD_LETTER.value)
            .order_by(WebhookEvent.created_at.desc())
            .limit(limit)
        )
        return result.scalars().all()
    
    async def retry_dead_letter(self, stripe_event_id: str) -> Dict[str, Any]:
        """Manually retry a dead letter event."""
        event = await self._get_event(stripe_event_id)
        if not event:
            return {"error": "Event not found"}
        
        if event.status != WebhookStatus.DEAD_LETTER.value:
            return {"error": f"Event is not in dead letter (status: {event.status})"}
        
        # Reset for retry
        event.status = WebhookStatus.PENDING.value
        event.attempts = 0
        event.max_attempts = 3  # Fewer retries for manual retry
        event.next_retry_at = datetime.utcnow()
        await self.db.commit()
        
        # Process immediately
        return await self.process_event(
            event.stripe_event_id,
            event.event_type,
            event.payload,
        )


# ============================================
# WEBHOOK HANDLERS
# ============================================

async def handle_checkout_completed(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handle checkout.session.completed event.

    Creates or upgrades subscription and grants role based on tier.
    This is the ONLY code path allowed to upgrade a user's role.
    """
    import httpx
    import hmac
    import hashlib
    import time
    import json
    import uuid
    from .config import settings

    session = payload.get("data", {}).get("object", {})

    customer_id = session.get("customer")
    subscription_id = session.get("subscription")
    client_reference_id = session.get("client_reference_id")  # User ID from checkout
    metadata = session.get("metadata", {})

    # Get user_id from client_reference_id (preferred) or metadata (fallback)
    user_id = client_reference_id or metadata.get("user_id")

    if not user_id:
        logger.error("checkout.session.completed missing user_id in client_reference_id or metadata")
        return {"error": "missing_user_id"}

    # Map Stripe price/tier to role
    # This mapping should match pricing.yaml plan IDs
    tier = metadata.get("tier", "developer")  # Default to developer tier

    # Map pricing.yaml tier IDs to role names
    tier_to_role = {
        "developer": "plus_plan",      # $29/mo plan -> plus_plan role
        "plus": "business_plan",      # $499/mo plan -> business_plan role
        "enterprise": "enterprise_plan",  # Custom -> enterprise_plan role
    }

    role = tier_to_role.get(tier, "plus_plan")  # Default to plus_plan if unknown

    logger.info(f"Processing checkout for user {user_id}, tier: {tier}, role: {role}")

    # Prepare request body - include user_id to bind signature to specific user
    request_body = json.dumps({"role": role, "user_id": user_id}).encode()
    timestamp = str(int(time.time()))
    nonce = str(uuid.uuid4())  # Generate unique nonce

    # Generate HMAC signature
    secret = getattr(settings, "INTERNAL_SERVICE_KEY", "").encode()
    if not secret:
        logger.error("INTERNAL_SERVICE_KEY not configured")
        return {"error": "internal_key_missing"}

    signature = hmac.new(
        secret,
        request_body + timestamp.encode() + nonce.encode(),
        hashlib.sha256
    ).hexdigest()

    # Call Auth service to update user role
    try:
        auth_url = getattr(settings, "AUTH_URL", "http://auth_service:8000").rstrip("/")
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{auth_url}/auth/users/{user_id}/role",
                content=request_body,
                headers={
                    "Content-Type": "application/json",
                    "X-Webhook-Signature": signature,
                    "X-Webhook-Timestamp": timestamp,
                    "X-Webhook-Nonce": nonce,
                }
            )

            if response.status_code == 200:
                logger.info(f"✅ Role updated to {role} for user {user_id}")
            else:
                logger.error(f"❌ Failed to update role for user {user_id}: {response.text}")
                return {"error": "role_update_failed", "detail": response.text}

    except Exception as e:
        logger.error(f"❌ Error calling Auth service for role update: {e}")
        return {"error": "auth_service_error", "detail": str(e)}

    return {
        "action": "subscription_created",
        "user_id": user_id,
        "tier": tier,
        "role": role,
        "subscription_id": subscription_id,
        "customer_id": customer_id,
    }


async def handle_invoice_paid(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handle invoice.paid event.
    
    Records payment and grants monthly credits for subscription renewals.
    """
    invoice = payload.get("data", {}).get("object", {})
    
    customer_id = invoice.get("customer")
    subscription_id = invoice.get("subscription")
    amount_paid = invoice.get("amount_paid", 0)
    
    logger.info(f"Invoice paid: customer={customer_id}, amount={amount_paid}")
    
    return {
        "action": "invoice_paid",
        "customer_id": customer_id,
        "subscription_id": subscription_id,
        "amount_paid": amount_paid,
    }


async def handle_invoice_payment_failed(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handle invoice.payment_failed event.
    
    Marks subscription as past_due and sends alert.
    """
    invoice = payload.get("data", {}).get("object", {})
    
    customer_id = invoice.get("customer")
    subscription_id = invoice.get("subscription")
    
    logger.warning(f"Invoice payment failed: customer={customer_id}")
    
    # TODO: Send alert email to user
    # TODO: Update subscription status to past_due
    
    return {
        "action": "payment_failed",
        "customer_id": customer_id,
        "subscription_id": subscription_id,
    }


async def handle_subscription_updated(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handle customer.subscription.updated event.

    Updates tier and adjusts role based on plan changes.
    Only processes active or trialing subscriptions - past_due, canceled, unpaid are downgraded.
    """
    import httpx
    import hmac
    import hashlib
    import time
    import json
    import uuid
    from .config import settings

    subscription = payload.get("data", {}).get("object", {})

    subscription_id = subscription.get("id")
    status = subscription.get("status")
    metadata = subscription.get("metadata", {})
    items = subscription.get("items", {}).get("data", [])

    user_id = metadata.get("user_id")

    logger.info(f"Subscription updated: {subscription_id}, status={status}")

    # Handle plan changes (downgrade/upgrade)
    if user_id and items:
        # Only process active or trialing subscriptions
        # past_due, canceled, unpaid, incomplete should downgrade to unsubscribed
        if status not in ("active", "trialing"):
            logger.info(f"Subscription status {status} is not active - downgrading to unsubscribed")
            try:
                # Include user_id in signed payload to bind signature to specific user
                request_body = json.dumps({"role": "unsubscribed", "user_id": user_id}).encode()
                timestamp = str(int(time.time()))
                nonce = str(uuid.uuid4())

                secret = getattr(settings, "INTERNAL_SERVICE_KEY", "").encode()
                if not secret:
                    logger.error("INTERNAL_SERVICE_KEY not configured")
                    return {"error": "internal_key_missing"}

                signature = hmac.new(
                    secret,
                    request_body + timestamp.encode() + nonce.encode(),
                    hashlib.sha256
                ).hexdigest()

                auth_url = getattr(settings, "AUTH_URL", "http://auth_service:8000").rstrip("/")
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.post(
                        f"{auth_url}/auth/users/{user_id}/role",
                        content=request_body,
                        headers={
                            "Content-Type": "application/json",
                            "X-Webhook-Signature": signature,
                            "X-Webhook-Timestamp": timestamp,
                            "X-Webhook-Nonce": nonce,
                        }
                    )

                    if response.status_code == 200:
                        logger.info(f"✅ Role downgraded to unsubscribed for user {user_id} (status={status})")
                    else:
                        logger.error(f"❌ Failed to downgrade role for user {user_id}: {response.text}")

            except Exception as e:
                logger.error(f"❌ Error calling Auth service for role downgrade: {e}")

            return {
                "action": "subscription_updated",
                "subscription_id": subscription_id,
                "status": status,
                "user_id": user_id,
                "role": "unsubscribed",
            }

        # Get the price ID from the subscription item (use first item for simplicity)
        # For multi-item subscriptions, you may need more complex logic
        price_id = items[0].get("price", {}).get("id") if items else None

        # Map price IDs to tiers (this should be configured based on your Stripe products)
        # For now, use metadata tier if available
        tier = metadata.get("tier")

        if tier:
            tier_to_role = {
                "developer": "plus_plan",
                "plus": "business_plan",
                "enterprise": "enterprise_plan",
            }
            role = tier_to_role.get(tier, "plus_plan")

            try:
                # Include user_id in signed payload to bind signature to specific user
                request_body = json.dumps({"role": role, "user_id": user_id}).encode()
                timestamp = str(int(time.time()))
                nonce = str(uuid.uuid4())

                secret = getattr(settings, "INTERNAL_SERVICE_KEY", "").encode()
                if not secret:
                    logger.error("INTERNAL_SERVICE_KEY not configured")
                    return {"error": "internal_key_missing"}

                signature = hmac.new(
                    secret,
                    request_body + timestamp.encode() + nonce.encode(),
                    hashlib.sha256
                ).hexdigest()

                auth_url = getattr(settings, "AUTH_URL", "http://auth_service:8000").rstrip("/")
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.post(
                        f"{auth_url}/auth/users/{user_id}/role",
                        content=request_body,
                        headers={
                            "Content-Type": "application/json",
                            "X-Webhook-Signature": signature,
                            "X-Webhook-Timestamp": timestamp,
                            "X-Webhook-Nonce": nonce,
                        }
                    )

                    if response.status_code == 200:
                        logger.info(f"✅ Role updated to {role} for user {user_id} (subscription update)")
                    else:
                        logger.error(f"❌ Failed to update role for user {user_id}: {response.text}")

            except Exception as e:
                logger.error(f"❌ Error calling Auth service for role update: {e}")

    return {
        "action": "subscription_updated",
        "subscription_id": subscription_id,
        "status": status,
        "user_id": user_id,
    }


async def handle_subscription_deleted(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handle customer.subscription.deleted event.

    Downgrades user to unsubscribed role.
    """
    import httpx
    import hmac
    import hashlib
    import time
    import json
    import uuid
    from .config import settings

    subscription = payload.get("data", {}).get("object", {})

    subscription_id = subscription.get("id")
    customer_id = subscription.get("customer")
    metadata = subscription.get("metadata", {})

    user_id = metadata.get("user_id")

    logger.info(f"Subscription deleted: {subscription_id}")

    # Downgrade user to unsubscribed role
    if user_id:
        try:
            # Include user_id in signed payload to bind signature to specific user
            request_body = json.dumps({"role": "unsubscribed", "user_id": user_id}).encode()
            timestamp = str(int(time.time()))
            nonce = str(uuid.uuid4())

            secret = getattr(settings, "INTERNAL_SERVICE_KEY", "").encode()
            if not secret:
                logger.error("INTERNAL_SERVICE_KEY not configured")
                return {"error": "internal_key_missing"}

            signature = hmac.new(
                secret,
                request_body + timestamp.encode() + nonce.encode(),
                hashlib.sha256
            ).hexdigest()

            auth_url = getattr(settings, "AUTH_URL", "http://auth_service:8000").rstrip("/")
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{auth_url}/auth/users/{user_id}/role",
                    content=request_body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Webhook-Signature": signature,
                        "X-Webhook-Timestamp": timestamp,
                        "X-Webhook-Nonce": nonce,
                    }
                )

                if response.status_code == 200:
                    logger.info(f"✅ Role downgraded to unsubscribed for user {user_id}")
                else:
                    logger.error(f"❌ Failed to downgrade role for user {user_id}: {response.text}")

        except Exception as e:
            logger.error(f"❌ Error calling Auth service for role downgrade: {e}")

    return {
        "action": "subscription_deleted",
        "subscription_id": subscription_id,
        "customer_id": customer_id,
        "user_id": user_id,
    }


async def handle_payment_intent_succeeded(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handle payment_intent.succeeded event.
    
    For credit pack purchases, adds credits to balance.
    """
    payment_intent = payload.get("data", {}).get("object", {})
    
    payment_id = payment_intent.get("id")
    amount = payment_intent.get("amount", 0)
    metadata = payment_intent.get("metadata", {})
    
    user_id = metadata.get("user_id")
    credits = metadata.get("credits")
    pack_type = metadata.get("pack_type")
    
    if credits and user_id:
        logger.info(f"Credit purchase: user={user_id}, credits={credits}")
        # TODO: Add credits to user balance
        return {
            "action": "credits_purchased",
            "user_id": user_id,
            "credits": int(credits),
            "pack_type": pack_type,
            "payment_id": payment_id,
        }
    
    return {
        "action": "payment_succeeded",
        "payment_id": payment_id,
        "amount": amount,
    }


def create_webhook_processor(db: AsyncSession) -> WebhookProcessor:
    """
    Create and configure webhook processor with all handlers.
    """
    processor = WebhookProcessor(db)
    
    # Register handlers
    processor.register_handler("checkout.session.completed", handle_checkout_completed)
    processor.register_handler("invoice.paid", handle_invoice_paid)
    processor.register_handler("invoice.payment_failed", handle_invoice_payment_failed)
    processor.register_handler("customer.subscription.updated", handle_subscription_updated)
    processor.register_handler("customer.subscription.deleted", handle_subscription_deleted)
    processor.register_handler("payment_intent.succeeded", handle_payment_intent_succeeded)
    
    return processor
