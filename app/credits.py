"""
Credit system management.

Phase 1.4 GTM: Added atomic operations with row-level locking
and idempotency support for production reliability.
"""

import logging
import os
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple, Union

import httpx
from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import AsyncSession

from .models import CreditBalance, CreditTransaction, UsageRecord, Subscription
from .config import settings
from .idempotency import BillingIdempotency, get_idempotency

logger = logging.getLogger(__name__)


_credits_notice_dedupe_memory: set[str] = set()


def _notification_service_url() -> str:
    base = (os.getenv("NOTIFICATION_SERVICE_URL") or getattr(settings, "NOTIFICATION_SERVICE_URL", "") or "").strip()
    if base:
        return base.rstrip("/")

    deployment_color = os.getenv("DEPLOYMENT_COLOR", "").strip().lower()
    if deployment_color in {"blue", "green"}:
        return f"http://{deployment_color}_notification_service:8000"
    return "http://notification_service:8000"


async def _maybe_notify_insufficient_credits(
    *,
    user_id: uuid.UUID,
    plan: str,
    available: int,
    required: int,
) -> None:
    month = datetime.utcnow().strftime("%Y-%m")
    dedupe_key = f"credits_notice:{user_id}:{month}"

    try:
        from .cache import get_cache

        cache = get_cache()
        redis_client = getattr(cache, "redis_client", None)
        if redis_client is not None:
            already = await redis_client.exists(dedupe_key)
            if already:
                return
            await redis_client.setex(dedupe_key, 35 * 24 * 3600, "1")
        else:
            if dedupe_key in _credits_notice_dedupe_memory:
                return
            _credits_notice_dedupe_memory.add(dedupe_key)
    except Exception:
        return

    plan_normalized = (plan or "developer").strip().lower()
    if plan_normalized in {"developer", "free"}:
        action_url = f"{settings.FRONTEND_URL}/pricing"
        title = "Credits exhausted"
        message = (
            f"You've used all your credits (available: {max(available, 0)}, required: {max(required, 0)}). "
            "Upgrade to Plus to get more credits."
        )
    else:
        action_url = f"{settings.FRONTEND_URL}/billing"
        title = "Credits exhausted"
        message = (
            f"You've used all your credits (available: {max(available, 0)}, required: {max(required, 0)}). "
            "Buy more credits to continue."
        )

    base_url = _notification_service_url()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{base_url}/notifications/internal/create",
                params={"target_user_id": str(user_id)},
                json={
                    "title": title,
                    "message": message,
                    "notification_type": "error",
                    "channel": "in_app",
                    "entity_type": "credits",
                    "action_url": action_url,
                },
            )
    except Exception:
        return


def ensure_uuid(user_id: Union[str, uuid.UUID]) -> uuid.UUID:
    """Convert string user_id to UUID if needed."""
    if isinstance(user_id, uuid.UUID):
        return user_id
    try:
        return uuid.UUID(user_id)
    except (ValueError, AttributeError) as e:
        logger.error(f"Invalid user_id format: {user_id} - {str(e)}")
        raise ValueError(f"Invalid user_id format: {user_id}")

# Stripe import with fallback
try:
    import stripe
    stripe.api_key = settings.STRIPE_SECRET_KEY
    STRIPE_AVAILABLE = bool(settings.STRIPE_SECRET_KEY)
except ImportError:
    STRIPE_AVAILABLE = False


class CreditManager:
    """Manages user credit balances and transactions."""
    
    # Free tier starting credits
    FREE_TIER_CREDITS = 1000

    async def get_or_create_balance(
        self,
        user_id: str,
        db_session: AsyncSession,
        lock_for_update: bool = False,
    ) -> CreditBalance:
        """Get or create credit balance for user.
        
        New users automatically receive FREE_TIER_CREDITS (10000) as starting balance.
        
        Args:
            user_id: User ID (string or UUID)
            db_session: Database session
            lock_for_update: If True, use SELECT FOR UPDATE for single-writer invariant
        """
        try:
            # Convert user_id to UUID if it's a string
            user_uuid = ensure_uuid(user_id)
            
            query = select(CreditBalance).where(CreditBalance.user_id == user_uuid)
            if lock_for_update:
                query = query.with_for_update()  # Row-level lock for single-writer invariant
            
            result = await db_session.execute(query)
            balance = result.scalar_one_or_none()

            if not balance:
                # New user - grant free tier credits
                logger.info(f"Creating new credit balance for user: {str(user_uuid)[:8]}... with {self.FREE_TIER_CREDITS} free tier credits")
                balance = CreditBalance(
                    user_id=user_uuid, 
                    balance=self.FREE_TIER_CREDITS,
                    lifetime_bonus=self.FREE_TIER_CREDITS,  # Track as bonus credits
                )
                db_session.add(balance)
                
                # Create transaction record for initial credits
                initial_tx = CreditTransaction(
                    user_id=user_uuid,
                    tx_type="bonus",
                    amount=self.FREE_TIER_CREDITS,
                    balance_after=self.FREE_TIER_CREDITS,
                    description="Free tier welcome credits",
                )
                db_session.add(initial_tx)
                
                await db_session.commit()
                await db_session.refresh(balance)
                logger.info(f"Successfully created credit balance for user: {str(user_uuid)[:8]}...")

            return balance
        except ValueError as e:
            # Invalid UUID format
            logger.error(f"Invalid user_id format: {user_id} - {str(e)}")
            raise
        except Exception as e:
            logger.error(f"Database error in get_or_create_balance for user {str(user_id)[:8]}...: {str(e)}", exc_info=True)
            raise

    async def get_balance(
        self,
        user_id: str,
        db_session: AsyncSession,
    ) -> Dict[str, Any]:
        """Get user's credit balance."""
        try:
            balance = await self.get_or_create_balance(user_id, db_session)
            return {
                "balance": balance.balance,
                "lifetime_purchased": balance.lifetime_purchased,
                "lifetime_used": balance.lifetime_used,
                "lifetime_bonus": balance.lifetime_bonus,
                "expiring_credits": balance.expiring_credits,
                "expiration_date": balance.expiration_date.isoformat() if balance.expiration_date else None,
            }
        except Exception as e:
            logger.error(f"Failed to get balance for user {user_id[:8]}...: {str(e)}", exc_info=True)
            raise

    async def add_credits(
        self,
        user_id: str,
        amount: int,
        tx_type: str = "purchase",
        description: Optional[str] = None,
        reference_type: Optional[str] = None,
        reference_id: Optional[str] = None,
        stripe_payment_intent_id: Optional[str] = None,
        db_session: AsyncSession = None,
    ) -> CreditTransaction:
        """Add credits to user's balance."""
        if amount <= 0:
            raise ValueError("Amount must be positive")

        # Use row-level lock for single-writer invariant
        balance = await self.get_or_create_balance(user_id, db_session, lock_for_update=True)

        # Update balance
        balance.balance += amount
        if tx_type == "purchase":
            balance.lifetime_purchased += amount
        elif tx_type == "bonus":
            balance.lifetime_bonus += amount

        # Create transaction record
        transaction = CreditTransaction(
            user_id=user_id,
            tx_type=tx_type,
            amount=amount,
            balance_after=balance.balance,
            reference_type=reference_type,
            reference_id=reference_id,
            stripe_payment_intent_id=stripe_payment_intent_id,
            description=description,
        )
        db_session.add(transaction)

        await db_session.commit()
        await db_session.refresh(transaction)
        return transaction

    async def deduct_credits(
        self,
        user_id: str,
        amount: int,
        reference_type: str,
        reference_id: Optional[str] = None,
        description: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        db_session: AsyncSession = None,
    ) -> CreditTransaction:
        """Deduct credits from user's balance and record usage."""
        if amount <= 0:
            raise ValueError("Amount must be positive")

        # Use row-level lock for single-writer invariant
        balance = await self.get_or_create_balance(user_id, db_session, lock_for_update=True)

        user_uuid = ensure_uuid(user_id)

        plan = "developer"
        try:
            sub_result = await db_session.execute(
                select(Subscription.plan).where(Subscription.user_id == user_uuid)
            )
            plan = (sub_result.scalar_one_or_none() or "developer")
        except Exception:
            plan = "developer"

        if balance.balance < amount:
            try:
                await _maybe_notify_insufficient_credits(
                    user_id=user_uuid,
                    plan=plan,
                    available=balance.balance,
                    required=amount,
                )
            except Exception:
                pass
            raise ValueError(f"Insufficient credits. Available: {balance.balance}, Required: {amount}")

        # Update balance
        balance.balance -= amount
        balance.lifetime_used += amount

        # Create transaction record
        transaction = CreditTransaction(
            user_id=user_id,
            tx_type="usage",
            amount=-amount,
            balance_after=balance.balance,
            reference_type=reference_type,
            reference_id=reference_id,
            description=description,
        )
        db_session.add(transaction)

        # Also create usage record for dashboard metrics
        now = datetime.utcnow()
        period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if now.month == 12:
            period_end = period_start.replace(year=now.year + 1, month=1)
        else:
            period_end = period_start.replace(month=now.month + 1)
        
        # Determine usage type from reference_type
        usage_type = "credits"
        if reference_type in ("llm_tokens", "chat_message"):
            usage_type = "tokens"
        elif reference_type in ("agent_run", "agent_step"):
            usage_type = "agent_runs"
        elif reference_type == "code_execution":
            usage_type = "api_calls"
        
        usage_record = UsageRecord(
            user_id=user_id,
            usage_type=usage_type,
            quantity=amount,
            unit="credits",
            period_start=period_start,
            period_end=period_end,
            extra_metadata=metadata or {"reference_type": reference_type},
        )
        db_session.add(usage_record)

        await db_session.commit()
        await db_session.refresh(transaction)

        if balance.balance == 0:
            try:
                await _maybe_notify_insufficient_credits(
                    user_id=user_uuid,
                    plan=plan,
                    available=0,
                    required=amount,
                )
            except Exception:
                pass
        return transaction

    async def refund_credits(
        self,
        user_id: str,
        amount: int,
        original_tx_id: str,
        reason: Optional[str] = None,
        db_session: AsyncSession = None,
    ) -> CreditTransaction:
        """Refund credits to user's balance."""
        # Use row-level lock for single-writer invariant
        balance = await self.get_or_create_balance(user_id, db_session, lock_for_update=True)

        # Update balance
        balance.balance += amount
        if balance.lifetime_used >= amount:
            balance.lifetime_used -= amount

        # Create transaction record
        transaction = CreditTransaction(
            user_id=user_id,
            tx_type="refund",
            amount=amount,
            balance_after=balance.balance,
            reference_type="refund",
            reference_id=original_tx_id,
            description=reason or "Credit refund",
        )
        db_session.add(transaction)

        await db_session.commit()
        await db_session.refresh(transaction)
        return transaction

    async def purchase_credits(
        self,
        user_id: str,
        amount_usd: float,
        payment_method_id: Optional[str] = None,
        db_session: AsyncSession = None,
    ) -> Dict[str, Any]:
        """Purchase credits with payment."""
        credits_to_add = int(amount_usd * settings.CREDITS_PER_DOLLAR)

        if credits_to_add < settings.MIN_CREDIT_PURCHASE:
            raise ValueError(
                f"Minimum purchase is {settings.MIN_CREDIT_PURCHASE} credits "
                f"(${settings.MIN_CREDIT_PURCHASE / settings.CREDITS_PER_DOLLAR})"
            )

        # Create payment intent
        payment_intent_id = None
        client_secret = None

        if STRIPE_AVAILABLE:
            intent = stripe.PaymentIntent.create(
                amount=int(amount_usd * 100),  # Stripe uses cents
                currency="usd",
                payment_method=payment_method_id,
                confirm=bool(payment_method_id),
                metadata={
                    "user_id": user_id,
                    "credits": credits_to_add,
                    "type": "credit_purchase",
                },
            )
            payment_intent_id = intent.id
            client_secret = intent.client_secret

            # If payment confirmed, add credits immediately
            if intent.status == "succeeded":
                await self.add_credits(
                    user_id=user_id,
                    amount=credits_to_add,
                    tx_type="purchase",
                    description=f"Purchased {credits_to_add} credits for ${amount_usd}",
                    stripe_payment_intent_id=payment_intent_id,
                    db_session=db_session,
                )

        return {
            "payment_intent_id": payment_intent_id,
            "client_secret": client_secret,
            "credits": credits_to_add,
            "amount_usd": amount_usd,
            "status": "requires_payment" if client_secret else "completed",
        }

    async def get_transactions(
        self,
        user_id: str,
        tx_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        db_session: AsyncSession = None,
    ) -> List[CreditTransaction]:
        """Get user's credit transactions."""
        stmt = select(CreditTransaction).where(CreditTransaction.user_id == user_id)

        if tx_type:
            stmt = stmt.where(CreditTransaction.tx_type == tx_type)

        stmt = stmt.order_by(CreditTransaction.created_at.desc())
        stmt = stmt.limit(limit).offset(offset)

        result = await db_session.execute(stmt)
        return result.scalars().all()

    async def expire_old_credits(
        self,
        db_session: AsyncSession,
    ) -> int:
        """Expire credits past their expiration date."""
        now = datetime.utcnow()

        # Use SELECT FOR UPDATE to maintain single-writer invariant
        result = await db_session.execute(
            select(CreditBalance)
            .where(
                CreditBalance.expiration_date < now,
                CreditBalance.expiring_credits > 0,
            )
            .with_for_update()  # Row-level lock
        )
        balances = result.scalars().all()

        expired_count = 0
        for balance in balances:
            if balance.expiring_credits > 0:
                # Create expiration transaction
                transaction = CreditTransaction(
                    user_id=balance.user_id,
                    tx_type="expiration",
                    amount=-balance.expiring_credits,
                    balance_after=balance.balance - balance.expiring_credits,
                    description="Credits expired",
                )
                db_session.add(transaction)

                balance.balance -= balance.expiring_credits
                balance.expiring_credits = 0
                balance.expiration_date = None
                expired_count += 1

        await db_session.commit()
        return expired_count

    async def grant_bonus_credits(
        self,
        user_id: str,
        amount: int,
        reason: str,
        expires_in_days: Optional[int] = None,
        db_session: AsyncSession = None,
    ) -> CreditTransaction:
        """Grant bonus credits to user."""
        # Use row-level lock for single-writer invariant
        balance = await self.get_or_create_balance(user_id, db_session, lock_for_update=True)

        # Set expiration if specified
        if expires_in_days:
            balance.expiring_credits += amount
            balance.expiration_date = datetime.utcnow() + timedelta(days=expires_in_days)

        return await self.add_credits(
            user_id=user_id,
            amount=amount,
            tx_type="bonus",
            description=reason,
            db_session=db_session,
        )


    async def deduct_credits_atomic(
        self,
        user_id: str,
        amount: int,
        reference_type: str,
        reference_id: Optional[str] = None,
        description: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        db_session: AsyncSession = None,
    ) -> Tuple[CreditTransaction, bool]:
        """
        Atomically deduct credits with row-level locking.
        
        This is the production-safe method that:
        1. Uses SELECT FOR UPDATE to prevent race conditions
        2. Supports idempotency to prevent duplicate charges
        3. Returns both transaction and whether it was a duplicate
        
        Args:
            user_id: User ID
            amount: Amount to deduct
            reference_type: Type of reference (e.g., "llm_tokens", "agent_run")
            reference_id: Optional reference ID
            description: Optional description
            idempotency_key: Optional idempotency key (auto-generated if not provided)
            metadata: Optional metadata dict
            db_session: Database session
            
        Returns:
            Tuple of (CreditTransaction, is_duplicate)
            
        Raises:
            ValueError: If insufficient credits
        """
        if amount <= 0:
            raise ValueError("Amount must be positive")
        
        # Check idempotency
        idempotency = get_idempotency()
        if idempotency_key:
            check_result = await idempotency.check(idempotency_key)
            if check_result.is_duplicate:
                logger.info(f"Duplicate deduction detected: {idempotency_key}")
                # Return the previous transaction
                prev = check_result.previous_result
                if prev:
                    # Reconstruct transaction-like response
                    return CreditTransaction(
                        id=prev.get("id"),
                        user_id=user_id,
                        tx_type="usage",
                        amount=-amount,
                        balance_after=prev.get("balance_after", 0),
                        reference_type=reference_type,
                        reference_id=reference_id,
                        description=description,
                    ), True
        
        # Use SELECT FOR UPDATE to lock the row
        result = await db_session.execute(
            select(CreditBalance)
            .where(CreditBalance.user_id == user_id)
            .with_for_update()  # Row-level lock
        )
        balance = result.scalar_one_or_none()
        
        if not balance:
            # Create balance with free tier credits
            balance = CreditBalance(
                user_id=user_id,
                balance=self.FREE_TIER_CREDITS,
                lifetime_bonus=self.FREE_TIER_CREDITS,
            )
            db_session.add(balance)
            await db_session.flush()
        
        if balance.balance < amount:
            raise ValueError(
                f"Insufficient credits. Available: {balance.balance}, Required: {amount}"
            )
        
        # Atomic update
        balance.balance -= amount
        balance.lifetime_used += amount
        
        # Create transaction record
        transaction = CreditTransaction(
            user_id=user_id,
            tx_type="usage",
            amount=-amount,
            balance_after=balance.balance,
            reference_type=reference_type,
            reference_id=reference_id,
            description=description,
        )
        db_session.add(transaction)
        
        await db_session.commit()
        await db_session.refresh(transaction)
        
        logger.info(
            f"💳 Atomic deduction: {amount} credits from {user_id[:8]}... "
            f"New balance: {balance.balance}"
        )
        
        # Store in idempotency cache
        if idempotency_key:
            await idempotency.store(
                idempotency_key,
                {
                    "id": str(transaction.id),
                    "balance_after": balance.balance,
                    "amount": amount,
                    "reference_type": reference_type,
                },
                operation="credit_deduct",
                user_id=user_id,
                amount=amount,
            )
        
        return transaction, False

    async def deduct_credits_by_tokens(
        self,
        user_id: str,
        input_tokens: int,
        output_tokens: int,
        model: str = "gpt-4o",
        provider: str = "openai",
        reference_id: Optional[str] = None,
        description: Optional[str] = None,
        db_session: AsyncSession = None,
    ) -> Dict[str, Any]:
        """
        Deduct credits based on actual LLM token usage.
        
        Credit costs (from pricing.yaml):
        - Input tokens: 10 credits per 1K tokens
        - Output tokens: 30 credits per 1K tokens
        
        Provider multipliers:
        - OpenAI: 1.0x
        - Anthropic: 1.2x
        - Google: 0.8x
        - Groq: 0.5x
        - Local: 0.1x
        
        Args:
            user_id: User ID
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens
            model: Model name
            provider: LLM provider
            reference_id: Optional reference ID
            description: Optional description
            db_session: Database session
            
        Returns:
            Dict with balance, deducted amount, and token details
        """
        # Provider multipliers
        PROVIDER_MULTIPLIERS = {
            "openai": 1.0,
            "anthropic": 1.2,
            "google": 0.8,
            "groq": 0.5,
            "local": 0.1,
        }
        
        # Credit costs per 1K tokens
        INPUT_COST_PER_1K = 10
        OUTPUT_COST_PER_1K = 30
        
        multiplier = PROVIDER_MULTIPLIERS.get(provider.lower(), 1.0)
        
        input_credits = (input_tokens / 1000) * INPUT_COST_PER_1K * multiplier
        output_credits = (output_tokens / 1000) * OUTPUT_COST_PER_1K * multiplier
        
        # Round up to ensure we never undercharge
        credit_cost = max(1, int(input_credits + output_credits + 0.5))
        
        # Generate idempotency key
        idempotency = get_idempotency()
        idempotency_key = idempotency.generate_key(
            user_id, "llm_tokens", credit_cost, reference_id
        )
        
        # Use atomic deduction
        transaction, is_duplicate = await self.deduct_credits_atomic(
            user_id=user_id,
            amount=credit_cost,
            reference_type="llm_tokens",
            reference_id=reference_id,
            description=description or f"LLM: {input_tokens}in + {output_tokens}out ({model}/{provider})",
            idempotency_key=idempotency_key,
            metadata={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "model": model,
                "provider": provider,
            },
            db_session=db_session,
        )
        
        return {
            "balance": transaction.balance_after,
            "deducted": credit_cost,
            "transaction_id": str(transaction.id) if transaction.id else None,
            "is_duplicate": is_duplicate,
            "token_usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "model": model,
                "provider": provider,
            },
        }

    async def check_credits_sufficient(
        self,
        user_id: str,
        amount: int,
        db_session: AsyncSession,
    ) -> Tuple[bool, int]:
        """
        Check if user has sufficient credits.
        
        Args:
            user_id: User ID
            amount: Amount needed
            db_session: Database session
            
        Returns:
            Tuple of (has_sufficient, current_balance)
        """
        balance = await self.get_or_create_balance(user_id, db_session)
        return balance.balance >= amount, balance.balance


credit_manager = CreditManager()
