"""Subscription management with Stripe."""

import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Subscription, PricingPlan, PaymentMethod, CreditTransaction
from .config import settings

logger = logging.getLogger(__name__)

# Stripe import with fallback
try:
    import stripe
    stripe.api_key = settings.STRIPE_SECRET_KEY
    STRIPE_AVAILABLE = bool(settings.STRIPE_SECRET_KEY)
except ImportError:
    STRIPE_AVAILABLE = False


class SubscriptionManager:
    """Manages user subscriptions."""

    PLAN_HIERARCHY = ["developer", "plus", "enterprise"]

    async def get_or_create_customer(
        self,
        user_id: str,
        email: str,
        name: Optional[str] = None,
        db_session: AsyncSession = None,
    ) -> str:
        """Get or create Stripe customer for user."""
        # Check existing subscription
        result = await db_session.execute(
            select(Subscription).where(Subscription.user_id == user_id)
        )
        subscription = result.scalars().first()

        if subscription and subscription.stripe_customer_id:
            return subscription.stripe_customer_id

        # Create Stripe customer
        customer_id = None
        if STRIPE_AVAILABLE:
            customer = stripe.Customer.create(
                email=email,
                name=name,
                metadata={"user_id": user_id},
            )
            customer_id = customer.id

        # Create or update subscription record
        if subscription:
            subscription.stripe_customer_id = customer_id
        else:
            subscription = Subscription(
                user_id=user_id,
                stripe_customer_id=customer_id,
                plan="developer",
                status="active",
            )
            db_session.add(subscription)

        await db_session.commit()
        return customer_id

    async def get_subscription(
        self,
        user_id: str,
        db_session: AsyncSession,
    ) -> Optional[Subscription]:
        """Get user's subscription."""
        result = await db_session.execute(
            select(Subscription).where(Subscription.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def create_subscription(
        self,
        user_id: str,
        plan: str,
        billing_cycle: str = "monthly",
        payment_method_id: Optional[str] = None,
        coupon_code: Optional[str] = None,
        db_session: AsyncSession = None,
    ) -> Subscription:
        """Create or upgrade subscription."""
        subscription = await self.get_subscription(user_id, db_session)
        if not subscription:
            raise ValueError("User has no subscription record")

        # Get pricing plan
        plan_result = await db_session.execute(
            select(PricingPlan).where(PricingPlan.name == plan)
        )
        pricing_plan = plan_result.scalar_one_or_none()

        # Get price ID
        price_id = None
        if pricing_plan:
            price_id = (
                pricing_plan.stripe_price_yearly_id
                if billing_cycle == "yearly"
                else pricing_plan.stripe_price_monthly_id
            )

        # Create Stripe subscription
        stripe_sub_id = None
        if STRIPE_AVAILABLE and subscription.stripe_customer_id and price_id:
            sub_params = {
                "customer": subscription.stripe_customer_id,
                "items": [{"price": price_id}],
                "metadata": {"user_id": user_id, "plan": plan},
            }

            if payment_method_id:
                sub_params["default_payment_method"] = payment_method_id

            if coupon_code:
                sub_params["coupon"] = coupon_code

            stripe_sub = stripe.Subscription.create(**sub_params)
            stripe_sub_id = stripe_sub.id

            subscription.current_period_start = datetime.fromtimestamp(
                stripe_sub.current_period_start
            )
            subscription.current_period_end = datetime.fromtimestamp(
                stripe_sub.current_period_end
            )

        # Update subscription
        subscription.stripe_subscription_id = stripe_sub_id
        subscription.plan = plan
        subscription.billing_cycle = billing_cycle
        subscription.price_id = price_id
        subscription.status = "active"

        if pricing_plan:
            subscription.amount = (
                pricing_plan.yearly_price
                if billing_cycle == "yearly"
                else pricing_plan.monthly_price
            )

        await db_session.commit()
        await db_session.refresh(subscription)
        return subscription

    async def cancel_subscription(
        self,
        user_id: str,
        at_period_end: bool = True,
        db_session: AsyncSession = None,
    ) -> Subscription:
        """Cancel subscription."""
        subscription = await self.get_subscription(user_id, db_session)
        if not subscription:
            raise ValueError("Subscription not found")

        # Cancel in Stripe
        if STRIPE_AVAILABLE and subscription.stripe_subscription_id:
            if at_period_end:
                stripe.Subscription.modify(
                    subscription.stripe_subscription_id,
                    cancel_at_period_end=True,
                )
            else:
                stripe.Subscription.delete(subscription.stripe_subscription_id)

        subscription.status = "canceled"
        subscription.canceled_at = datetime.utcnow()

        await db_session.commit()
        return subscription

    async def reactivate_subscription(
        self,
        user_id: str,
        db_session: AsyncSession,
    ) -> Subscription:
        """Reactivate a canceled subscription."""
        subscription = await self.get_subscription(user_id, db_session)
        if not subscription:
            raise ValueError("Subscription not found")

        if STRIPE_AVAILABLE and subscription.stripe_subscription_id:
            stripe.Subscription.modify(
                subscription.stripe_subscription_id,
                cancel_at_period_end=False,
            )

        subscription.status = "active"
        subscription.canceled_at = None

        await db_session.commit()
        return subscription

    async def change_plan(
        self,
        user_id: str,
        new_plan: str,
        db_session: AsyncSession,
    ) -> Subscription:
        """Change subscription plan (upgrade/downgrade)."""
        subscription = await self.get_subscription(user_id, db_session)
        if not subscription:
            raise ValueError("Subscription not found")

        # Get new pricing plan
        plan_result = await db_session.execute(
            select(PricingPlan).where(PricingPlan.name == new_plan)
        )
        pricing_plan = plan_result.scalar_one_or_none()
        if not pricing_plan:
            raise ValueError(f"Plan {new_plan} not found")

        price_id = (
            pricing_plan.stripe_price_yearly_id
            if subscription.billing_cycle == "yearly"
            else pricing_plan.stripe_price_monthly_id
        )

        # Update in Stripe
        if STRIPE_AVAILABLE and subscription.stripe_subscription_id and price_id:
            stripe_sub = stripe.Subscription.retrieve(subscription.stripe_subscription_id)
            stripe.Subscription.modify(
                subscription.stripe_subscription_id,
                items=[{
                    "id": stripe_sub["items"]["data"][0].id,
                    "price": price_id,
                }],
                proration_behavior="create_prorations",
            )

        subscription.plan = new_plan
        subscription.price_id = price_id
        subscription.amount = (
            pricing_plan.yearly_price
            if subscription.billing_cycle == "yearly"
            else pricing_plan.monthly_price
        )

        # ============================================
        # CRITICAL: Update economic state and grant credits
        # ============================================
        from .economic_state import UserEconomicState, SubscriptionTier, TIER_DEFAULTS
        from .models import CreditTransaction
        
        result = await db_session.execute(
            select(UserEconomicState)
            .where(UserEconomicState.user_id == user_id)
            .with_for_update()  # Lock for update
        )
        state = result.scalar_one_or_none()
        
        if state:
            # Map plan name to tier
            tier_map = {
                "developer": SubscriptionTier.DEVELOPER,
                "plus": SubscriptionTier.PLUS,
                "enterprise": SubscriptionTier.ENTERPRISE,
            }
            new_tier = tier_map.get(new_plan.lower(), SubscriptionTier.DEVELOPER)
            old_tier = state.subscription_tier
            
            # Get tier defaults
            new_tier_defaults = TIER_DEFAULTS.get(new_tier, {})
            old_tier_defaults = TIER_DEFAULTS.get(old_tier, {})
            
            # Reset credits to new tier's monthly allocation
            new_tier_credits = TIER_DEFAULTS.get(new_tier, {}).get("credit_balance", 0)
            
            if new_tier_credits > 0:
                # Set to new tier's monthly allocation (e.g., 75,000 for Plus)
                old_balance = state.credit_balance
                state.credit_balance = new_tier_credits
                
                # Log the credit reset
                credit_tx = CreditTransaction(
                    user_id=state.user_id,
                    org_id=state.org_id,
                    amount=new_tier_credits - old_balance,
                    transaction_type="subscription_change",
                    description=f"Monthly credits reset: {old_tier} → {new_tier}",
                    balance_after=state.credit_balance,
                )
                db_session.add(credit_tx)
                
                logger.info(
                    f"Reset credits to {new_tier_credits} for tier {new_tier} "
                    f"(was {old_balance}, user: {state.user_id})"
                )

            # Update tier
            state.subscription_tier = new_tier

        await db_session.commit()
        await db_session.refresh(subscription)
        return subscription
    
    async def start_trial(
        self,
        user_id: str,
        plan: str,
        trial_days: int = 14,
        db_session: AsyncSession = None,
    ) -> Subscription:
        """Start a trial subscription."""
        subscription = await self.get_subscription(user_id, db_session)
        if not subscription:
            raise ValueError("Subscription not found")

        now = datetime.utcnow()
        trial_end = now + timedelta(days=trial_days)

        subscription.plan = plan
        subscription.status = "trialing"
        subscription.trial_start = now
        subscription.trial_end = trial_end

        await db_session.commit()
        return subscription

    async def handle_webhook(
        self,
        event_type: str,
        event_data: Dict[str, Any],
        db_session: AsyncSession,
    ) -> Dict[str, Any]:
        """Handle Stripe webhook events."""
        handlers = {
            "customer.subscription.created": self._handle_subscription_created,
            "customer.subscription.updated": self._handle_subscription_updated,
            "customer.subscription.deleted": self._handle_subscription_deleted,
            "invoice.paid": self._handle_invoice_paid,
            "invoice.payment_failed": self._handle_payment_failed,
            "checkout.session.completed": self._handle_checkout_completed,
            "invoice.upcoming": self._handle_invoice_upcoming,
        }

        handler = handlers.get(event_type)
        if handler:
            return await handler(event_data, db_session)

        return {"status": "ignored", "event": event_type}

    async def _handle_subscription_created(
        self, data: Dict[str, Any], db_session: AsyncSession
    ) -> Dict[str, Any]:
        """Handle subscription created event."""
        stripe_sub = data.get("data", {}).get("object", {})
        customer_id = stripe_sub.get("customer")

        result = await db_session.execute(
            select(Subscription).where(Subscription.stripe_customer_id == customer_id)
        )
        subscription = result.scalars().first()

        if subscription:
            subscription.stripe_subscription_id = stripe_sub.get("id")
            subscription.status = stripe_sub.get("status")
            subscription.current_period_start = datetime.fromtimestamp(
                stripe_sub.get("current_period_start", 0), tz=timezone.utc
            )
            subscription.current_period_end = datetime.fromtimestamp(
                stripe_sub.get("current_period_end", 0), tz=timezone.utc
            )

            # If Stripe created the subscription in `trialing` state, the user
            # is in their 30-day free window. Grant the plan's monthly credits
            # now so they can actually use the product during the trial. The
            # grant is idempotent on the subscription id, so a replayed webhook
            # or the subsequent `subscription.updated` won't double-grant.
            stripe_status = stripe_sub.get("status")
            if stripe_status == "trialing":
                # Pull plan_id from subscription metadata set at checkout.
                sub_meta = stripe_sub.get("metadata") or {}
                plan_id = sub_meta.get("plan_id") or subscription.plan or "developer"
                subscription.plan = plan_id

                # trial_end from Stripe is the authoritative end-of-trial time.
                trial_end = stripe_sub.get("trial_end")
                if trial_end:
                    subscription.trial_end = datetime.fromtimestamp(trial_end, tz=timezone.utc)
                    subscription.trial_start = datetime.now(timezone.utc)

                await db_session.commit()

                await self._grant_plan_credits(
                    subscription,
                    reason="trial_start",
                    reference_id=f"trial:{subscription.stripe_subscription_id}",
                    db_session=db_session,
                )
            else:
                await db_session.commit()

        return {"status": "processed", "event": "subscription.created"}


    async def _handle_subscription_updated(
        self, data: Dict[str, Any], db_session: AsyncSession
    ) -> Dict[str, Any]:
        """Handle subscription updated event."""
        stripe_sub = data.get("data", {}).get("object", {})
        sub_id = stripe_sub.get("id")

        result = await db_session.execute(
            select(Subscription).where(Subscription.stripe_subscription_id == sub_id)
        )
        subscription = result.scalars().first()

        if subscription:
            subscription.status = stripe_sub.get("status")
            subscription.current_period_end = datetime.fromtimestamp(
                stripe_sub.get("current_period_end", 0), tz=timezone.utc
            )
            await db_session.commit()

        return {"status": "processed", "event": "subscription.updated"}

    async def _handle_subscription_deleted(
        self, data: Dict[str, Any], db_session: AsyncSession
    ) -> Dict[str, Any]:
        """Handle subscription deleted event."""
        stripe_sub = data.get("data", {}).get("object", {})
        sub_id = stripe_sub.get("id")

        result = await db_session.execute(
            select(Subscription).where(Subscription.stripe_subscription_id == sub_id)
        )
        subscription = result.scalars().first()

        if subscription:
            subscription.status = "canceled"
            subscription.plan = "developer"
            await db_session.commit()

        return {"status": "processed", "event": "subscription.deleted"}

    async def _grant_plan_credits(
        self,
        subscription: "Subscription",
        *,
        reason: str,
        reference_id: Optional[str] = None,
        db_session: AsyncSession = None,
    ) -> Optional[Dict[str, Any]]:
        """Grant a plan's monthly included credits to the subscriber.

        Used both at trial start (so the user can actually use the product
        during the free month) and on every `invoice.paid` renewal (so each
        time Stripe charges them, they receive that month's credit allotment).

        Idempotency: every grant is recorded as a `CreditTransaction` whose
        `reference_id` is derived from the subscription + billing period, so a
        replayed webhook or duplicate invoice cannot double-grant. We also
        fall back to the generic idempotency service keyed on the stripe
        invoice/subscription id.

        Args:
            subscription: The user's Subscription row (carries user_id + plan).
            reason: Human-readable reason ("trial_start", "renewal", ...).
            reference_id: Stable unique key for this grant (e.g. stripe invoice id
                or "{sub_id}:{period_end}"). Required for correct idempotency.
            db_session: DB session.

        Returns:
            {"granted": int, "tx_id": str} on success, {"skipped": True} when a
            grant for this reference already exists.
        """
        if subscription is None or not subscription.user_id:
            logger.warning("grant_plan_credits: missing subscription/user_id")
            return {"skipped": True, "reason": "no_subscription"}

        if not reference_id:
            # Last-resort key so we never silently double-grant.
            reference_id = f"sub:{subscription.stripe_subscription_id or subscription.id}"

        # 1) Idempotency via existing CreditTransaction reference_id.
        #    reference_type marks plan-grant transactions; reference_id is the
        #    unique period/invoice key.
        existing = await db_session.execute(
            select(CreditTransaction).where(
                CreditTransaction.user_id == subscription.user_id,
                CreditTransaction.reference_type == "plan_grant",
                CreditTransaction.reference_id == reference_id,
            )
        )
        if existing.scalar_one_or_none():
            logger.info(
                "grant_plan_credits: already granted for ref=%s (user=%s)",
                reference_id, str(subscription.user_id)[:8],
            )
            return {"skipped": True, "reason": "already_granted"}

        # 2) Resolve the plan's included credits from pricing.yaml.
        from .pricing_loader import get_plan_credits
        plan_id = subscription.plan or "developer"
        credits = get_plan_credits(plan_id)
        if not credits or credits <= 0:
            logger.info(
                "grant_plan_credits: plan %s has no included credits (user=%s)",
                plan_id, str(subscription.user_id)[:8],
            )
            return {"skipped": True, "reason": "no_plan_credits"}

        # 3) Grant via credit_manager (row-locked, writes its own tx record).
        #    We tag our tx with reference_type="plan_grant" + reference_id so
        #    the check above catches replays. add_credits commits internally.
        from .credits import credit_manager
        tx = await credit_manager.add_credits(
            user_id=str(subscription.user_id),
            amount=credits,
            tx_type="bonus",
            description=f"{plan_id} plan credits — {reason}",
            reference_type="plan_grant",
            reference_id=reference_id,
            db_session=db_session,
        )

        logger.info(
            "grant_plan_credits: granted %d credits to user=%s plan=%s reason=%s ref=%s",
            credits, str(subscription.user_id)[:8], plan_id, reason, reference_id,
        )
        return {"granted": credits, "tx_id": str(tx.id)}

    async def _handle_invoice_paid(
        self, data: Dict[str, Any], db_session: AsyncSession
    ) -> Dict[str, Any]:
        """Handle invoice paid event.

        Stripe fires `invoice.paid` whenever a payment succeeds — both the
        first real charge at the end of a trial AND every subsequent monthly
        renewal. We grant the plan's included credits for that period here so
        paying users receive their monthly allotment.
        """
        invoice = data.get("data", {}).get("object", {})
        customer_id = invoice.get("customer")
        invoice_id = invoice.get("id")

        # Locate the local subscription by Stripe customer id.
        result = await db_session.execute(
            select(Subscription).where(Subscription.stripe_customer_id == customer_id)
        )
        subscription = result.scalars().first()
        if not subscription:
            logger.warning("invoice.paid: no subscription for customer %s", customer_id)
            return {"status": "processed", "event": "invoice.paid", "granted": False}

        # Refresh period dates from the invoice so the local row stays in sync.
        ps = invoice.get("period_start")
        pe = invoice.get("period_end")
        if ps:
            subscription.current_period_start = datetime.fromtimestamp(ps, tz=timezone.utc)
        if pe:
            subscription.current_period_end = datetime.fromtimestamp(pe, tz=timezone.utc)
        if subscription.status == "trialing":
            # First paid invoice ends the trial window.
            subscription.status = "active"
        await db_session.commit()

        # The invoice id is globally unique at Stripe, so it makes a safe
        # idempotency key for this grant.
        grant = await self._grant_plan_credits(
            subscription,
            reason="renewal",
            reference_id=f"invoice:{invoice_id}",
            db_session=db_session,
        )

        return {"status": "processed", "event": "invoice.paid", "credits": grant}

    async def _handle_checkout_completed(
        self, data: Dict[str, Any], db_session: AsyncSession
    ) -> Dict[str, Any]:
        """Handle checkout.session.completed event.
        
        Creates or updates subscription from checkout session metadata.
        """
        from .economic_state import UserEconomicState, SubscriptionTier, SubscriptionStatus, SubscriptionSource, TIER_DEFAULTS
        
        session = data.get("data", {}).get("object", {})
        customer_id = session.get("customer")
        subscription_id = session.get("subscription")
        metadata = session.get("metadata", {})
        
        user_id = metadata.get("user_id")
        plan_id = metadata.get("plan_id", "developer")
        billing_cycle = metadata.get("billing_cycle", "monthly")
        org_id = metadata.get("org_id")
        
        if not user_id:
            logger.error("checkout.session.completed missing user_id in metadata")
            return {"status": "error", "event": "checkout.session.completed", "error": "missing_user_id"}
        
        logger.info(f"Processing checkout for user {user_id}, plan: {plan_id}")
        
        # Map plan_id to SubscriptionTier enum
        tier_map = {
            "developer": SubscriptionTier.DEVELOPER,
            "plus": SubscriptionTier.PLUS,
            "enterprise": SubscriptionTier.ENTERPRISE,
        }
        tier_enum = tier_map.get(plan_id.lower(), SubscriptionTier.DEVELOPER)
        
        # Check if subscription already exists for this customer
        result = await db_session.execute(
            select(Subscription).where(Subscription.stripe_customer_id == customer_id)
        )
        subscription = result.scalars().first()
        
        if subscription:
            # Update existing subscription
            subscription.stripe_subscription_id = subscription_id
            subscription.plan = plan_id
            subscription.billing_cycle = billing_cycle
            subscription.status = "trialing" if metadata.get("trial") == "True" else "active"
            
            # Set trial dates if applicable
            if metadata.get("trial") == "True":
                subscription.trial_start = datetime.now(timezone.utc)
                subscription.trial_end = datetime.now(timezone.utc) + timedelta(days=30)
        else:
            # Create new subscription
            subscription = Subscription(
                user_id=uuid.UUID(user_id),
                stripe_customer_id=customer_id,
                stripe_subscription_id=subscription_id,
                plan=plan_id,
                billing_cycle=billing_cycle,
                status="trialing" if metadata.get("trial") == "True" else "active",
            )
            
            # Set trial dates if applicable
            if metadata.get("trial") == "True":
                subscription.trial_start = datetime.now(timezone.utc)
                subscription.trial_end = datetime.now(timezone.utc) + timedelta(days=30)
            
            db_session.add(subscription)
        
        await db_session.commit()
        
        # Create or update UserEconomicState
        result = await db_session.execute(
            select(UserEconomicState).where(UserEconomicState.user_id == uuid.UUID(user_id))
        )
        economic_state = result.scalars().first()
        
        tier_defaults = TIER_DEFAULTS.get(tier_enum, TIER_DEFAULTS[SubscriptionTier.DEVELOPER])
        
        if economic_state:
            # Update existing state
            economic_state.subscription_tier = tier_enum
            economic_state.subscription_status = SubscriptionStatus.ACTIVE
            economic_state.subscription_source = SubscriptionSource.STRIPE
            economic_state.subscription_id = subscription_id
            economic_state.credit_balance = tier_defaults.get("credit_balance", 29_000)
            economic_state.credit_rate = tier_defaults.get("credit_rate", 1.0)
        else:
            # Create new UserEconomicState
            if not org_id:
                logger.error("checkout.session.completed missing org_id in metadata")
                return {"status": "error", "event": "checkout.session.completed", "error": "missing_org_id"}
            
            economic_state = UserEconomicState(
                user_id=uuid.UUID(user_id),
                org_id=uuid.UUID(org_id),
                subscription_tier=tier_enum,
                subscription_status=SubscriptionStatus.ACTIVE,
                subscription_source=SubscriptionSource.STRIPE,
                subscription_id=subscription_id,
                credit_balance=tier_defaults.get("credit_balance", 29_000),
                credit_rate=tier_defaults.get("credit_rate", 1.0),
            )
            db_session.add(economic_state)
        
        await db_session.commit()
        
        # Grant trial credits if applicable
        if metadata.get("trial") == "True":
            await self._grant_plan_credits(
                subscription,
                reason="trial_start",
                reference_id=f"trial:{subscription_id}",
                db_session=db_session,
            )
        
        return {"status": "processed", "event": "checkout.session.completed", "subscription_id": subscription_id}

    async def _handle_invoice_upcoming(
        self, data: Dict[str, Any], db_session: AsyncSession
    ) -> Dict[str, Any]:
        """Handle invoice.upcoming event.
        
        Sent 3-7 days before invoice is due. Used for notifications.
        """
        invoice = data.get("data", {}).get("object", {})
        customer_id = invoice.get("customer")
        amount_due = invoice.get("amount_due", 0)
        currency = invoice.get("currency", "usd")
        due_date = invoice.get("due_date")
        
        logger.info(
            f"Invoice upcoming for customer {customer_id}: {amount_due/100:.2f} {currency.upper()} "
            f"due on {datetime.fromtimestamp(due_date) if due_date else 'unknown'}"
        )
        
        # This event is typically used for sending notifications to users
        # For now, we just log it. Future implementation could:
        # - Send email notifications
        # - Update user dashboard with upcoming payment info
        # - Trigger payment method reminders
        
        return {"status": "processed", "event": "invoice.upcoming"}


    async def _handle_payment_failed(
        self, data: Dict[str, Any], db_session: AsyncSession
    ) -> Dict[str, Any]:
        """Handle payment failed event."""
        invoice = data.get("data", {}).get("object", {})
        customer_id = invoice.get("customer")

        result = await db_session.execute(
            select(Subscription).where(Subscription.stripe_customer_id == customer_id)
        )
        subscription = result.scalars().first()

        if subscription:
            subscription.status = "past_due"
            await db_session.commit()

        return {"status": "processed", "event": "invoice.payment_failed"}


subscription_manager = SubscriptionManager()
