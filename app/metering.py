"""Usage metering for Stripe billing."""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from .models import UsageRecord, Subscription
from .config import settings

# Stripe import with fallback
try:
    import stripe
    stripe.api_key = settings.STRIPE_SECRET_KEY
    STRIPE_AVAILABLE = bool(settings.STRIPE_SECRET_KEY)
except ImportError:
    STRIPE_AVAILABLE = False


class UsageMeter:
    """Tracks and reports usage to Stripe for metered billing."""

    # Usage type to Stripe meter mapping
    METER_MAPPING = {
        "tokens": settings.STRIPE_METER_TOKENS,
        "agent_runs": settings.STRIPE_METER_AGENT_RUNS,
        "api_calls": settings.STRIPE_METER_API_CALLS,
    }

    # Unit prices (per unit)
    UNIT_PRICES = {
        "tokens": Decimal("0.00001"),  # $0.01 per 1000 tokens
        "agent_runs": Decimal("0.05"),  # $0.05 per agent run
        "api_calls": Decimal("0.001"),  # $0.001 per API call
    }

    async def record_usage(
        self,
        user_id: str,
        usage_type: str,
        quantity: int,
        metadata: Optional[Dict[str, Any]] = None,
        db_session: AsyncSession = None,
    ) -> UsageRecord:
        """Record usage for a user."""
        # Get subscription
        result = await db_session.execute(
            select(Subscription).where(Subscription.user_id == user_id)
        )
        subscription = result.scalar_one_or_none()

        now = datetime.utcnow()
        period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if now.month == 12:
            period_end = period_start.replace(year=now.year + 1, month=1)
        else:
            period_end = period_start.replace(month=now.month + 1)

        # Calculate cost
        unit_price = self.UNIT_PRICES.get(usage_type, Decimal("0"))
        total_cost = unit_price * quantity

        # Create usage record
        record = UsageRecord(
            user_id=user_id,
            subscription_id=subscription.id if subscription else None,
            usage_type=usage_type,
            quantity=quantity,
            unit=usage_type,
            period_start=period_start,
            period_end=period_end,
            unit_price=unit_price,
            total_cost=total_cost,
            metadata=metadata,
        )
        db_session.add(record)
        await db_session.commit()
        await db_session.refresh(record)

        return record

    async def report_to_stripe(
        self,
        user_id: str,
        usage_type: str,
        db_session: AsyncSession,
    ) -> Dict[str, Any]:
        """Report unreported usage to Stripe."""
        if not STRIPE_AVAILABLE:
            return {"status": "skipped", "reason": "Stripe not configured"}

        # Get subscription
        result = await db_session.execute(
            select(Subscription).where(Subscription.user_id == user_id)
        )
        subscription = result.scalar_one_or_none()

        if not subscription or not subscription.stripe_subscription_id:
            return {"status": "skipped", "reason": "No active subscription"}

        # Get unreported usage
        result = await db_session.execute(
            select(UsageRecord).where(
                UsageRecord.user_id == user_id,
                UsageRecord.usage_type == usage_type,
                UsageRecord.reported_to_stripe == False,
            )
        )
        records = result.scalars().all()

        if not records:
            return {"status": "skipped", "reason": "No unreported usage"}

        # Aggregate usage
        total_quantity = sum(r.quantity for r in records)

        # Get meter ID
        meter_id = self.METER_MAPPING.get(usage_type)
        if not meter_id:
            return {"status": "skipped", "reason": f"No meter configured for {usage_type}"}

        # Report to Stripe
        try:
            # Get subscription item for the meter
            stripe_sub = stripe.Subscription.retrieve(subscription.stripe_subscription_id)
            subscription_item_id = None

            for item in stripe_sub["items"]["data"]:
                if item.get("price", {}).get("recurring", {}).get("meter") == meter_id:
                    subscription_item_id = item.id
                    break

            if subscription_item_id:
                usage_record = stripe.SubscriptionItem.create_usage_record(
                    subscription_item_id,
                    quantity=total_quantity,
                    timestamp=int(datetime.utcnow().timestamp()),
                    action="increment",
                )

                # Mark records as reported
                for record in records:
                    record.reported_to_stripe = True
                    record.stripe_usage_record_id = usage_record.id

                await db_session.commit()

                return {
                    "status": "reported",
                    "quantity": total_quantity,
                    "usage_record_id": usage_record.id,
                }

        except stripe.error.StripeError as e:
            return {"status": "error", "error": str(e)}

        return {"status": "skipped", "reason": "No matching subscription item"}

    async def get_usage_summary(
        self,
        user_id: str,
        period_start: Optional[datetime] = None,
        period_end: Optional[datetime] = None,
        db_session: AsyncSession = None,
    ) -> Dict[str, Any]:
        """Get usage summary for a user."""
        if not period_start:
            now = datetime.utcnow()
            period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if not period_end:
            period_end = datetime.utcnow()

        # Aggregate by usage type
        result = await db_session.execute(
            select(
                UsageRecord.usage_type,
                func.sum(UsageRecord.quantity).label("total_quantity"),
                func.sum(UsageRecord.total_cost).label("total_cost"),
            )
            .where(
                UsageRecord.user_id == user_id,
                UsageRecord.created_at >= period_start,
                UsageRecord.created_at <= period_end,
            )
            .group_by(UsageRecord.usage_type)
        )
        rows = result.all()

        summary = {
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "usage": {},
            "total_cost": Decimal("0"),
        }

        for row in rows:
            summary["usage"][row.usage_type] = {
                "quantity": row.total_quantity or 0,
                "cost": float(row.total_cost or 0),
            }
            summary["total_cost"] += row.total_cost or Decimal("0")

        summary["total_cost"] = float(summary["total_cost"])
        return summary

    async def get_usage_history(
        self,
        user_id: str,
        usage_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        db_session: AsyncSession = None,
    ) -> List[UsageRecord]:
        """Get usage history for a user."""
        stmt = select(UsageRecord).where(UsageRecord.user_id == user_id)

        if usage_type:
            stmt = stmt.where(UsageRecord.usage_type == usage_type)

        stmt = stmt.order_by(UsageRecord.created_at.desc())
        stmt = stmt.limit(limit).offset(offset)

        result = await db_session.execute(stmt)
        return result.scalars().all()

    async def check_usage_limits(
        self,
        user_id: str,
        usage_type: str,
        requested_quantity: int,
        db_session: AsyncSession,
    ) -> Dict[str, Any]:
        """Check if user has exceeded usage limits for their plan."""
        # Get subscription
        result = await db_session.execute(
            select(Subscription).where(Subscription.user_id == user_id)
        )
        subscription = result.scalar_one_or_none()

        plan = subscription.plan if subscription else "developer"

        # Plan limits
        PLAN_LIMITS = {
            "developer": {"tokens": 50000, "agent_runs": 10, "api_calls": 1000},
            "plus": {"tokens": 5000000, "agent_runs": 1000, "api_calls": 100000},
            "enterprise": {"tokens": 50000000, "agent_runs": 10000, "api_calls": 1000000},
        }

        limits = PLAN_LIMITS.get(plan, PLAN_LIMITS["developer"])
        limit = limits.get(usage_type, 0)

        # Get current period usage
        now = datetime.utcnow()
        period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        result = await db_session.execute(
            select(func.sum(UsageRecord.quantity))
            .where(
                UsageRecord.user_id == user_id,
                UsageRecord.usage_type == usage_type,
                UsageRecord.created_at >= period_start,
            )
        )
        current_usage = result.scalar() or 0

        remaining = max(0, limit - current_usage)
        would_exceed = (current_usage + requested_quantity) > limit

        return {
            "plan": plan,
            "usage_type": usage_type,
            "limit": limit,
            "current_usage": current_usage,
            "remaining": remaining,
            "requested": requested_quantity,
            "would_exceed": would_exceed,
            "allowed": not would_exceed,
        }


usage_meter = UsageMeter()
