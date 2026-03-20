"""
Stripe → UserEconomicState Integration

This module handles the mapping between Stripe events and UserEconomicState.

Rules:
  - Stripe is NOT authoritative (it charges money, emits events)
  - Billing is authoritative (owns truth, mutates UserEconomicState)
  - Tiers: DEVELOPER (free), PLUS ($499/mo), ENTERPRISE (custom)

Event Flow:
  checkout.session.completed → upgrade_to_tier() + set status=active + add credits
  invoice.payment_failed → set status=past_due + enforcement_mode=warn
  customer.subscription.deleted → set status=canceled + downgrade to DEVELOPER
"""

import logging
import os
from typing import Optional, Dict, Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .economic_state import (
    UserEconomicState,
    SubscriptionTier,
    SubscriptionStatus,
    SubscriptionSource,
    EnforcementMode,
    TIER_DEFAULTS,
)
from .config import settings

logger = logging.getLogger(__name__)


# ============================================
# STRIPE PRICE ID → TIER MAPPING
# ============================================

# These must match your Stripe dashboard exactly
# Matches frontend signupLogic.ts: developer (free), plus ($499/mo), enterprise (custom)
# Price IDs from setup_stripe_products.py
STRIPE_PRICE_TO_TIER: Dict[str, SubscriptionTier] = {
    # Developer (free) - no Stripe price needed
    
    # Plus - $499/month, $4990/year (LIVE price IDs)
    "price_1ShbBTCOOYDd7mWixaWdcmxt": SubscriptionTier.PLUS,  # Monthly
    "price_1ShbBTCOOYDd7mWiAjyrmyU9": SubscriptionTier.PLUS,  # Yearly
    
    # Enterprise - custom pricing (handled via sales)
}

# Reverse mapping for creating Stripe subscriptions
TIER_TO_STRIPE_PRICE: Dict[SubscriptionTier, Dict[str, str]] = {
    SubscriptionTier.DEVELOPER: {
        "monthly": None,  # Free tier - no Stripe subscription
        "yearly": None,
    },
    SubscriptionTier.PLUS: {
        "monthly": "price_1ShbBTCOOYDd7mWixaWdcmxt",
        "yearly": "price_1ShbBTCOOYDd7mWiAjyrmyU9",
    },
    SubscriptionTier.ENTERPRISE: {
        "monthly": None,  # Custom pricing - contact sales
        "yearly": None,
    },
    # API Subscriptions - Use environment variables
    SubscriptionTier.STATE_PHYSICS_DEV: {
        "monthly": os.getenv("STRIPE_PRICE_STATE_PHYSICS_DEV"),
        "yearly": None,  # API subscriptions are monthly only
    },
    SubscriptionTier.STATE_PHYSICS_STARTUP: {
        "monthly": os.getenv("STRIPE_PRICE_STATE_PHYSICS_STARTUP"),
        "yearly": None,
    },
    SubscriptionTier.HASH_SPHERE_DEV: {
        "monthly": os.getenv("STRIPE_PRICE_HASH_SPHERE_DEV"),
        "yearly": None,
    },
    SubscriptionTier.HASH_SPHERE_STARTUP: {
        "monthly": os.getenv("STRIPE_PRICE_HASH_SPHERE_STARTUP"),
        "yearly": None,
    },
}


def get_tier_from_price_id(price_id: str) -> SubscriptionTier:
    """
    Map a Stripe price ID to a subscription tier.
    
    Returns DEVELOPER if price ID is unknown.
    """
    return STRIPE_PRICE_TO_TIER.get(price_id, SubscriptionTier.DEVELOPER)


def get_price_id_for_tier(tier: SubscriptionTier, billing_cycle: str = "monthly") -> Optional[str]:
    """
    Get the Stripe price ID for a tier and billing cycle.
    Returns None for free tier (no Stripe subscription needed).
    """
    return TIER_TO_STRIPE_PRICE.get(tier, {}).get(billing_cycle)


# ============================================
# WEBHOOK HANDLERS
# ============================================

async def handle_checkout_completed(
    session: AsyncSession,
    event_data: Dict[str, Any],
) -> Optional[UserEconomicState]:
    """
    Handle checkout.session.completed webhook.
    
    This is called when a user successfully completes a Stripe checkout.
    
    Actions:
    1. Extract user_id from metadata
    2. Get price_id and map to tier
    3. Upgrade user to new tier
    4. Set subscription_status = active
    5. Add bonus credits for upgrade
    """
    checkout_session = event_data.get("object", {})
    
    # Extract metadata
    metadata = checkout_session.get("metadata", {})
    user_id_str = metadata.get("user_id")
    
    if not user_id_str:
        logger.error("checkout.session.completed missing user_id in metadata")
        return None
    
    try:
        user_id = UUID(user_id_str)
    except ValueError:
        logger.error(f"Invalid user_id in metadata: {user_id_str}")
        return None
    
    # Get subscription details
    subscription_id = checkout_session.get("subscription")
    
    # Get line items to determine price
    line_items = checkout_session.get("line_items", {}).get("data", [])
    if not line_items:
        logger.warning("No line items in checkout session")
        return None
    
    price_id = line_items[0].get("price", {}).get("id")
    if not price_id:
        logger.warning("No price_id in line items")
        return None
    
    # Map to tier
    new_tier = get_tier_from_price_id(price_id)
    
    # Get user's economic state WITH ROW-LEVEL LOCK
    # This maintains the single-writer economic invariant
    result = await session.execute(
        select(UserEconomicState)
        .where(UserEconomicState.user_id == user_id)
        .with_for_update()  # Row-level lock
    )
    state = result.scalar_one_or_none()
    
    if not state:
        logger.error(f"No economic state found for user {user_id}")
        return None
    
    # Upgrade tier (row is locked, so this is safe)
    old_tier = state.subscription_tier
    state.upgrade_to_tier(new_tier)
    
    # Update subscription info
    state.subscription_id = subscription_id
    state.subscription_source = SubscriptionSource.STRIPE
    state.subscription_status = SubscriptionStatus.ACTIVE
    state.enforcement_mode = EnforcementMode.STRICT
    
    await session.commit()
    await session.refresh(state)
    
    logger.info(f"User {user_id} upgraded from {old_tier.value} to {new_tier.value}")
    
    return state


async def handle_invoice_payment_failed(
    session: AsyncSession,
    event_data: Dict[str, Any],
) -> Optional[UserEconomicState]:
    """
    Handle invoice.payment_failed webhook.
    
    This is called when a payment fails (card declined, etc).
    
    Actions:
    1. Set subscription_status = past_due
    2. Set enforcement_mode = warn (allow usage but log warnings)
    """
    invoice = event_data.get("object", {})
    
    # Get customer ID
    customer_id = invoice.get("customer")
    subscription_id = invoice.get("subscription")
    
    if not subscription_id:
        logger.warning("invoice.payment_failed missing subscription_id")
        return None
    
    # Find user by subscription_id WITH ROW-LEVEL LOCK
    # This maintains the single-writer economic invariant
    result = await session.execute(
        select(UserEconomicState)
        .where(UserEconomicState.subscription_id == subscription_id)
        .with_for_update()  # Row-level lock
    )
    state = result.scalar_one_or_none()
    
    if not state:
        logger.warning(f"No economic state found for subscription {subscription_id}")
        return None
    
    # Update status (row is locked, so this is safe)
    state.subscription_status = SubscriptionStatus.PAST_DUE
    state.enforcement_mode = EnforcementMode.WARN
    
    await session.commit()
    await session.refresh(state)
    
    logger.warning(f"User {state.user_id} payment failed, status set to past_due")
    
    return state


async def handle_subscription_deleted(
    session: AsyncSession,
    event_data: Dict[str, Any],
) -> Optional[UserEconomicState]:
    """
    Handle customer.subscription.deleted webhook.
    
    This is called when a subscription is canceled (immediately or at period end).
    
    Actions:
    1. Set subscription_status = canceled
    2. Downgrade to FREE tier
    3. Keep existing credits (don't punish)
    """
    subscription = event_data.get("object", {})
    subscription_id = subscription.get("id")
    
    if not subscription_id:
        logger.warning("customer.subscription.deleted missing subscription_id")
        return None
    
    # Find user by subscription_id WITH ROW-LEVEL LOCK
    # This maintains the single-writer economic invariant
    result = await session.execute(
        select(UserEconomicState)
        .where(UserEconomicState.subscription_id == subscription_id)
        .with_for_update()  # Row-level lock
    )
    state = result.scalar_one_or_none()
    
    if not state:
        logger.warning(f"No economic state found for subscription {subscription_id}")
        return None
    
    # Update status
    old_tier = state.subscription_tier
    state.subscription_status = SubscriptionStatus.CANCELED
    
    # Downgrade to DEVELOPER tier (but keep credits)
    current_credits = state.credit_balance
    state.subscription_tier = SubscriptionTier.DEVELOPER
    state.hard_limits = TIER_DEFAULTS[SubscriptionTier.DEVELOPER]["hard_limits"]
    state.soft_limits = TIER_DEFAULTS[SubscriptionTier.DEVELOPER]["soft_limits"]
    state.features = TIER_DEFAULTS[SubscriptionTier.DEVELOPER]["features"]
    state.credit_rate = TIER_DEFAULTS[SubscriptionTier.DEVELOPER]["credit_rate"]
    state.credit_balance = current_credits  # Keep existing credits
    
    # Clear subscription reference
    state.subscription_id = None
    state.subscription_source = SubscriptionSource.INTERNAL
    
    await session.commit()
    await session.refresh(state)
    
    logger.info(f"User {state.user_id} subscription canceled, downgraded from {old_tier.value} to DEVELOPER")
    
    return state


async def handle_subscription_updated(
    session: AsyncSession,
    event_data: Dict[str, Any],
) -> Optional[UserEconomicState]:
    """
    Handle customer.subscription.updated webhook.
    
    This is called when a subscription is modified (plan change, etc).
    
    Actions:
    1. Check if plan changed
    2. If so, upgrade/downgrade tier
    """
    subscription = event_data.get("object", {})
    subscription_id = subscription.get("id")
    
    if not subscription_id:
        return None
    
    # Get new price
    items = subscription.get("items", {}).get("data", [])
    if not items:
        return None
    
    price_id = items[0].get("price", {}).get("id")
    if not price_id:
        return None
    
    new_tier = get_tier_from_price_id(price_id)
    
    # Find user by subscription_id WITH ROW-LEVEL LOCK
    # This maintains the single-writer economic invariant
    result = await session.execute(
        select(UserEconomicState)
        .where(UserEconomicState.subscription_id == subscription_id)
        .with_for_update()  # Row-level lock
    )
    state = result.scalar_one_or_none()
    
    if not state:
        return None
    
    # Check if tier changed (row is locked, so this is safe)
    if state.subscription_tier != new_tier:
        old_tier = state.subscription_tier
        state.upgrade_to_tier(new_tier)
        
        logger.info(f"User {state.user_id} plan changed from {old_tier.value} to {new_tier.value}")
    
    # Update status based on subscription status
    stripe_status = subscription.get("status")
    if stripe_status == "active":
        state.subscription_status = SubscriptionStatus.ACTIVE
        state.enforcement_mode = EnforcementMode.STRICT
    elif stripe_status == "past_due":
        state.subscription_status = SubscriptionStatus.PAST_DUE
        state.enforcement_mode = EnforcementMode.WARN
    elif stripe_status in ("canceled", "unpaid"):
        state.subscription_status = SubscriptionStatus.CANCELED
    
    await session.commit()
    await session.refresh(state)
    
    return state


async def handle_invoice_paid(
    session: AsyncSession,
    event_data: Dict[str, Any],
) -> Optional[UserEconomicState]:
    """
    Handle invoice.paid webhook.
    
    This is called when an invoice is successfully paid.
    
    Actions:
    1. Set subscription_status = active (if was past_due)
    2. Set enforcement_mode = strict
    3. Add monthly credit allocation (if applicable)
    """
    invoice = event_data.get("object", {})
    subscription_id = invoice.get("subscription")
    
    if not subscription_id:
        return None
    
    # Find user by subscription_id WITH ROW-LEVEL LOCK
    # This maintains the single-writer economic invariant
    result = await session.execute(
        select(UserEconomicState)
        .where(UserEconomicState.subscription_id == subscription_id)
        .with_for_update()  # Row-level lock
    )
    state = result.scalar_one_or_none()
    
    if not state:
        return None
    
    # Restore active status
    if state.subscription_status == SubscriptionStatus.PAST_DUE:
        state.subscription_status = SubscriptionStatus.ACTIVE
        state.enforcement_mode = EnforcementMode.STRICT
        
        logger.info(f"User {state.user_id} payment received, status restored to active")
    
    # Add monthly credit allocation based on tier
    tier_credits = TIER_DEFAULTS[state.subscription_tier]["credit_balance"]
    monthly_allocation = tier_credits // 12  # Rough monthly allocation
    
    if monthly_allocation > 0:
        state.credit_balance += monthly_allocation
        logger.info(f"User {state.user_id} received {monthly_allocation} monthly credits")
    
    await session.commit()
    await session.refresh(state)
    
    return state


# ============================================
# WEBHOOK ROUTER
# ============================================

WEBHOOK_HANDLERS = {
    "checkout.session.completed": handle_checkout_completed,
    "invoice.payment_failed": handle_invoice_payment_failed,
    "customer.subscription.deleted": handle_subscription_deleted,
    "customer.subscription.updated": handle_subscription_updated,
    "invoice.paid": handle_invoice_paid,
}


async def process_stripe_webhook(
    session: AsyncSession,
    event_type: str,
    event_data: Dict[str, Any],
) -> Optional[UserEconomicState]:
    """
    Process a Stripe webhook event.
    
    Returns the updated UserEconomicState if applicable.
    """
    handler = WEBHOOK_HANDLERS.get(event_type)
    
    if not handler:
        logger.debug(f"No handler for Stripe event: {event_type}")
        return None
    
    return await handler(session, event_data)
