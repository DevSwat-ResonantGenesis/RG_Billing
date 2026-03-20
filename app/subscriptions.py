"""Subscription management with Stripe."""

import secrets
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Subscription, PricingPlan, PaymentMethod
from .config import settings

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
        subscription = result.scalar_one_or_none()

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
        }

        handler = handlers.get(event_type)
        if handler:
            return await handler(event_data, db_session)

        return {"status": "ignored", "event": event_type}

    async def _handle_subscription_created(
        self, data: Dict[str, Any], db_session: AsyncSession
    ) -> Dict[str, Any]:
        """Handle subscription created event."""
        stripe_sub = data.get("object", {})
        customer_id = stripe_sub.get("customer")

        result = await db_session.execute(
            select(Subscription).where(Subscription.stripe_customer_id == customer_id)
        )
        subscription = result.scalar_one_or_none()

        if subscription:
            subscription.stripe_subscription_id = stripe_sub.get("id")
            subscription.status = stripe_sub.get("status")
            subscription.current_period_start = datetime.fromtimestamp(
                stripe_sub.get("current_period_start", 0)
            )
            subscription.current_period_end = datetime.fromtimestamp(
                stripe_sub.get("current_period_end", 0)
            )
            await db_session.commit()

        return {"status": "processed", "event": "subscription.created"}

    async def _handle_subscription_updated(
        self, data: Dict[str, Any], db_session: AsyncSession
    ) -> Dict[str, Any]:
        """Handle subscription updated event."""
        stripe_sub = data.get("object", {})
        sub_id = stripe_sub.get("id")

        result = await db_session.execute(
            select(Subscription).where(Subscription.stripe_subscription_id == sub_id)
        )
        subscription = result.scalar_one_or_none()

        if subscription:
            subscription.status = stripe_sub.get("status")
            subscription.current_period_end = datetime.fromtimestamp(
                stripe_sub.get("current_period_end", 0)
            )
            await db_session.commit()

        return {"status": "processed", "event": "subscription.updated"}

    async def _handle_subscription_deleted(
        self, data: Dict[str, Any], db_session: AsyncSession
    ) -> Dict[str, Any]:
        """Handle subscription deleted event."""
        stripe_sub = data.get("object", {})
        sub_id = stripe_sub.get("id")

        result = await db_session.execute(
            select(Subscription).where(Subscription.stripe_subscription_id == sub_id)
        )
        subscription = result.scalar_one_or_none()

        if subscription:
            subscription.status = "canceled"
            subscription.plan = "developer"
            await db_session.commit()

        return {"status": "processed", "event": "subscription.deleted"}

    async def _handle_invoice_paid(
        self, data: Dict[str, Any], db_session: AsyncSession
    ) -> Dict[str, Any]:
        """Handle invoice paid event."""
        return {"status": "processed", "event": "invoice.paid"}

    async def _handle_payment_failed(
        self, data: Dict[str, Any], db_session: AsyncSession
    ) -> Dict[str, Any]:
        """Handle payment failed event."""
        invoice = data.get("object", {})
        customer_id = invoice.get("customer")

        result = await db_session.execute(
            select(Subscription).where(Subscription.stripe_customer_id == customer_id)
        )
        subscription = result.scalar_one_or_none()

        if subscription:
            subscription.status = "past_due"
            await db_session.commit()

        return {"status": "processed", "event": "invoice.payment_failed"}


subscription_manager = SubscriptionManager()
