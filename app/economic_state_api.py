"""
Gateway ↔ Billing API Contract for UserEconomicState.

This module provides the API endpoints that the gateway MUST call
to read and enforce economic state before processing requests.

Contract:
- Gateway calls GET /economic-state/{user_id} on every authenticated request
- Gateway receives headers to inject into downstream requests
- Gateway calls POST /economic-state/{user_id}/deduct after execution
- Only billing_service can modify UserEconomicState
"""

import os
import logging
from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import httpx

from .db import get_session
from .cache import get_cache
from .config import settings
from .economic_state import (
    UserEconomicState,
    SubscriptionTier,
    SubscriptionStatus,
    SubscriptionSource,
    EnforcementMode,
    TIER_DEFAULTS,
)


router = APIRouter(prefix="/economic-state", tags=["economic-state"])

logger = logging.getLogger(__name__)

_exhausted_dedupe_memory: set[str] = set()


def _notification_service_url() -> str:
    base = (settings.NOTIFICATION_SERVICE_URL or "").strip()
    if base:
        return base.rstrip("/")

    deployment_color = os.getenv("DEPLOYMENT_COLOR", "").strip().lower()
    if deployment_color in {"blue", "green"}:
        return f"http://{deployment_color}_notification_service:8000"
    return "http://notification_service:8000"


async def _maybe_notify_insufficient_credits(
    *,
    user_id: str,
    tier: SubscriptionTier,
    available: int,
    required: int,
) -> None:
    if available < 0:
        available = 0
    if required < 0:
        required = 0

    month = datetime.utcnow().strftime("%Y-%m")
    dedupe_key = f"credits_notice:{user_id}:{month}"

    cache = get_cache()
    redis_client = getattr(cache, "redis_client", None)

    try:
        if redis_client is not None:
            already = await redis_client.exists(dedupe_key)
            if already:
                return
            await redis_client.setex(dedupe_key, 35 * 24 * 3600, "1")
        else:
            if dedupe_key in _exhausted_dedupe_memory:
                return
            _exhausted_dedupe_memory.add(dedupe_key)
    except Exception:
        return

    if tier == SubscriptionTier.DEVELOPER:
        action_url = f"{settings.FRONTEND_URL}/pricing"
        title = "Credits exhausted"
        message = (
            f"You've used all your credits (available: {available}, required: {required}). "
            "Upgrade to Plus to get more credits."
        )
    else:
        action_url = f"{settings.FRONTEND_URL}/billing"
        title = "Credits exhausted"
        message = (
            f"You've used all your credits (available: {available}, required: {required}). "
            "Buy more credits to continue."
        )

    base_url = _notification_service_url()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{base_url}/notifications/internal/create",
                params={"target_user_id": user_id},
                json={
                    "title": title,
                    "message": message,
                    "notification_type": "warning" if available > 0 else "error",
                    "channel": "in_app",
                    "entity_type": "credits",
                    "action_url": action_url,
                },
            )
            if resp.status_code >= 400:
                logger.warning(
                    "Failed to create credits notification for user %s: %s",
                    user_id,
                    resp.text,
                )
    except Exception:
        return


# ============================================
# REQUEST/RESPONSE MODELS (must be defined before endpoints)
# ============================================

class EconomicStateResponse(BaseModel):
    """Full economic state for dashboard/admin views."""
    id: str
    user_id: str
    org_id: str
    subscription_tier: str
    subscription_status: str
    subscription_source: str
    subscription_id: Optional[str]
    credit_balance: int
    credit_rate: float
    hard_limits: dict
    soft_limits: dict
    features: dict
    enforcement_mode: str
    is_dev_override: bool
    created_at: Optional[str]
    updated_at: Optional[str]


class GatewayHeadersResponse(BaseModel):
    """Minimal response for gateway to inject headers."""
    headers: dict
    allowed: bool
    reason: str


class CreateEconomicStateRequest(BaseModel):
    """Request to create economic state during registration."""
    user_id: str
    org_id: str
    tier: str = "developer"
    subscription_source: str = "internal"
    subscription_id: Optional[str] = None
    is_dev_override: bool = False


class DeductCreditsRequest(BaseModel):
    """Request to deduct credits after execution."""
    amount: int
    reference_type: str
    reference_id: Optional[str] = None
    description: Optional[str] = None


class DeductCreditsResponse(BaseModel):
    """Response after credit deduction."""
    success: bool
    new_balance: int
    amount_deducted: int
    reason: str


class CheckLimitRequest(BaseModel):
    """Request to check if a limit would be exceeded."""
    limit_name: str
    current_value: int


class CheckLimitResponse(BaseModel):
    """Response for limit check."""
    allowed: bool
    limit: int
    current: int
    reason: str


class CheckFeatureResponse(BaseModel):
    """Response for feature access check."""
    allowed: bool
    feature: str
    reason: str


# ============================================
# /ME ENDPOINTS (for authenticated user)
# ============================================

@router.get("/me", response_model=EconomicStateResponse)
async def get_my_economic_state(
    x_user_id: str = Header(..., alias="X-User-Id"),
    session: AsyncSession = Depends(get_session),
):
    """
    Get economic state for the currently authenticated user.
    
    Uses X-User-Id header injected by gateway auth middleware.
    This is the primary endpoint for frontend dashboards.
    """
    try:
        user_uuid = UUID(x_user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user_id format")

    result = await session.execute(
        select(UserEconomicState).where(UserEconomicState.user_id == user_uuid)
    )
    state = result.scalar_one_or_none()

    if not state:
        raise HTTPException(status_code=404, detail="Economic state not found")

    return EconomicStateResponse(**state.to_dict())


@router.get("/me/check-feature/{feature_name}", response_model=CheckFeatureResponse)
async def check_my_feature(
    feature_name: str,
    x_user_id: str = Header(..., alias="X-User-Id"),
    session: AsyncSession = Depends(get_session),
):
    """Check if current user can access a specific feature."""
    try:
        user_uuid = UUID(x_user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user_id format")

    result = await session.execute(
        select(UserEconomicState).where(UserEconomicState.user_id == user_uuid)
    )
    state = result.scalar_one_or_none()

    if not state:
        raise HTTPException(status_code=404, detail="Economic state not found")

    allowed = state.can_access_feature(feature_name)

    return CheckFeatureResponse(
        allowed=allowed,
        feature=feature_name,
        reason="" if allowed else f"Feature '{feature_name}' not available on {state.subscription_tier.value} tier"
    )


@router.post("/me/check-limit", response_model=CheckLimitResponse)
async def check_my_limit(
    request: CheckLimitRequest,
    x_user_id: str = Header(..., alias="X-User-Id"),
    session: AsyncSession = Depends(get_session),
):
    """Check if a limit would be exceeded for current user."""
    try:
        user_uuid = UUID(x_user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user_id format")

    result = await session.execute(
        select(UserEconomicState).where(UserEconomicState.user_id == user_uuid)
    )
    state = result.scalar_one_or_none()

    if not state:
        raise HTTPException(status_code=404, detail="Economic state not found")

    allowed, reason = state.check_hard_limit(request.limit_name, request.current_value)
    limit = state.hard_limits.get(request.limit_name, 0)

    return CheckLimitResponse(
        allowed=allowed,
        limit=limit,
        current=request.current_value,
        reason=reason
    )


@router.post("/me/check-credits", response_model=DeductCreditsResponse)
async def check_my_credits(
    amount: int,
    x_user_id: str = Header(..., alias="X-User-Id"),
    session: AsyncSession = Depends(get_session),
):
    """Check if current user has sufficient credits WITHOUT deducting."""
    try:
        user_uuid = UUID(x_user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user_id format")

    result = await session.execute(
        select(UserEconomicState).where(UserEconomicState.user_id == user_uuid)
    )
    state = result.scalar_one_or_none()

    if not state:
        raise HTTPException(status_code=404, detail="Economic state not found")

    effective_cost = int(amount * state.credit_rate)
    
    if state.is_dev_override or state.credit_rate == 0.0:
        return DeductCreditsResponse(
            success=True,
            new_balance=state.credit_balance,
            amount_deducted=0,
            reason=""
        )
    
    if state.credit_balance >= effective_cost:
        return DeductCreditsResponse(
            success=True,
            new_balance=state.credit_balance - effective_cost,
            amount_deducted=effective_cost,
            reason=""
        )
    
    if state.enforcement_mode == EnforcementMode.STRICT:
        await _maybe_notify_insufficient_credits(
            user_id=user_id,
            tier=state.subscription_tier,
            available=state.credit_balance,
            required=effective_cost,
        )
        return DeductCreditsResponse(
            success=False,
            new_balance=state.credit_balance,
            amount_deducted=0,
            reason=f"Insufficient credits ({state.credit_balance} < {effective_cost})"
        )
    
    return DeductCreditsResponse(
        success=True,
        new_balance=state.credit_balance - effective_cost,
        amount_deducted=effective_cost,
        reason=f"Warning: would result in negative balance"
    )


# ============================================
# GATEWAY ENDPOINTS (called on every request)
# ============================================

@router.get("/{user_id}/headers", response_model=GatewayHeadersResponse)
async def get_gateway_headers(
    user_id: str,
    session: AsyncSession = Depends(get_session),
):
    """
    PRIMARY GATEWAY ENDPOINT.
    
    Called by gateway on EVERY authenticated request.
    Returns headers to inject into downstream requests.
    
    If user has no economic state, returns allowed=False.
    Gateway MUST reject the request if allowed=False.
    """
    try:
        user_uuid = UUID(user_id)
    except ValueError:
        return GatewayHeadersResponse(
            headers={},
            allowed=False,
            reason="Invalid user_id format"
        )

    result = await session.execute(
        select(UserEconomicState).where(UserEconomicState.user_id == user_uuid)
    )
    state = result.scalar_one_or_none()

    if not state:
        return GatewayHeadersResponse(
            headers={},
            allowed=False,
            reason="No economic state found for user"
        )

    # Check subscription status
    if state.subscription_status == SubscriptionStatus.SUSPENDED:
        return GatewayHeadersResponse(
            headers=state.to_gateway_headers(),
            allowed=False,
            reason="Subscription suspended"
        )

    if state.subscription_status == SubscriptionStatus.CANCELED:
        return GatewayHeadersResponse(
            headers=state.to_gateway_headers(),
            allowed=False,
            reason="Subscription canceled"
        )

    return GatewayHeadersResponse(
        headers=state.to_gateway_headers(),
        allowed=True,
        reason=""
    )


@router.get("/{user_id}", response_model=EconomicStateResponse)
async def get_economic_state(
    user_id: str,
    session: AsyncSession = Depends(get_session),
):
    """
    Get full economic state for a user.
    Used by dashboards and admin views.
    """
    try:
        user_uuid = UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user_id format")

    result = await session.execute(
        select(UserEconomicState).where(UserEconomicState.user_id == user_uuid)
    )
    state = result.scalar_one_or_none()

    if not state:
        raise HTTPException(status_code=404, detail="Economic state not found")

    return EconomicStateResponse(**state.to_dict())


# ============================================
# REGISTRATION ENDPOINT (called during signup)
# ============================================

@router.post("/", response_model=EconomicStateResponse)
async def create_economic_state(
    request: CreateEconomicStateRequest,
    session: AsyncSession = Depends(get_session),
):
    """
    Create economic state for a new user.
    
    MUST be called during registration.
    Registration MUST fail if this fails.
    
    This is the ONLY way a user enters the economic system.
    """
    try:
        user_uuid = UUID(request.user_id)
        org_uuid = UUID(request.org_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid UUID format")

    # Check if already exists
    result = await session.execute(
        select(UserEconomicState).where(UserEconomicState.user_id == user_uuid)
    )
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Economic state already exists for user")

    # Map tier string to enum
    try:
        tier = SubscriptionTier(request.tier.lower())
    except ValueError:
        tier = SubscriptionTier.DEVELOPER

    # Map source string to enum
    try:
        source = SubscriptionSource(request.subscription_source.lower())
    except ValueError:
        source = SubscriptionSource.INTERNAL

    # Create state
    state = UserEconomicState.create_for_tier(
        user_id=user_uuid,
        org_id=org_uuid,
        tier=tier,
        subscription_source=source,
        subscription_id=request.subscription_id,
        is_dev_override=request.is_dev_override,
    )

    session.add(state)
    await session.commit()
    await session.refresh(state)

    return EconomicStateResponse(**state.to_dict())


# ============================================
# CREDIT ENFORCEMENT ENDPOINTS
# ============================================

@router.post("/{user_id}/deduct", response_model=DeductCreditsResponse)
async def deduct_credits(
    user_id: str,
    request: DeductCreditsRequest,
    session: AsyncSession = Depends(get_session),
):
    """
    Deduct credits after execution.
    
    Called by gateway AFTER a request completes successfully.
    The actual cost is: amount * credit_rate
    
    Returns success=False if insufficient credits (in strict mode).
    """
    try:
        user_uuid = UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user_id format")

    # Use SELECT FOR UPDATE to maintain single-writer economic invariant
    result = await session.execute(
        select(UserEconomicState)
        .where(UserEconomicState.user_id == user_uuid)
        .with_for_update()  # Row-level lock for atomic deduction
    )
    state = result.scalar_one_or_none()

    if not state:
        raise HTTPException(status_code=404, detail="Economic state not found")

    # Calculate effective cost
    effective_cost = int(request.amount * state.credit_rate)
    
    # Attempt deduction (row is locked, so this is safe)
    success, reason = state.deduct_credits(request.amount)
    
    if success:
        await session.commit()
        await session.refresh(state)
        if state.credit_balance == 0 and not state.is_dev_override and state.credit_rate != 0.0:
            await _maybe_notify_insufficient_credits(
                user_id=user_id,
                tier=state.subscription_tier,
                available=0,
                required=effective_cost,
            )
    else:
        if state.enforcement_mode == EnforcementMode.STRICT:
            await _maybe_notify_insufficient_credits(
                user_id=user_id,
                tier=state.subscription_tier,
                available=state.credit_balance,
                required=effective_cost,
            )

    return DeductCreditsResponse(
        success=success,
        new_balance=state.credit_balance,
        amount_deducted=effective_cost if success else 0,
        reason=reason
    )


@router.post("/{user_id}/check-credits", response_model=DeductCreditsResponse)
async def check_credits(
    user_id: str,
    amount: int,
    session: AsyncSession = Depends(get_session),
):
    """
    Check if user has sufficient credits WITHOUT deducting.
    
    Called by gateway BEFORE execution to pre-validate.
    """
    try:
        user_uuid = UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user_id format")

    result = await session.execute(
        select(UserEconomicState).where(UserEconomicState.user_id == user_uuid)
    )
    state = result.scalar_one_or_none()

    if not state:
        raise HTTPException(status_code=404, detail="Economic state not found")

    # Calculate effective cost
    effective_cost = int(amount * state.credit_rate)
    
    # Check without deducting
    if state.is_dev_override or state.credit_rate == 0.0:
        return DeductCreditsResponse(
            success=True,
            new_balance=state.credit_balance,
            amount_deducted=0,
            reason=""
        )
    
    if state.credit_balance >= effective_cost:
        return DeductCreditsResponse(
            success=True,
            new_balance=state.credit_balance - effective_cost,
            amount_deducted=effective_cost,
            reason=""
        )
    
    if state.enforcement_mode == EnforcementMode.STRICT:
        return DeductCreditsResponse(
            success=False,
            new_balance=state.credit_balance,
            amount_deducted=0,
            reason=f"Insufficient credits ({state.credit_balance} < {effective_cost})"
        )
    
    # WARN mode
    return DeductCreditsResponse(
        success=True,
        new_balance=state.credit_balance - effective_cost,
        amount_deducted=effective_cost,
        reason=f"Warning: would result in negative balance"
    )


# ============================================
# LIMIT ENFORCEMENT ENDPOINTS
# ============================================

@router.post("/{user_id}/check-limit", response_model=CheckLimitResponse)
async def check_limit(
    user_id: str,
    request: CheckLimitRequest,
    session: AsyncSession = Depends(get_session),
):
    """
    Check if a hard limit would be exceeded.
    
    Called by gateway/services before creating agents, workflows, etc.
    """
    try:
        user_uuid = UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user_id format")

    result = await session.execute(
        select(UserEconomicState).where(UserEconomicState.user_id == user_uuid)
    )
    state = result.scalar_one_or_none()

    if not state:
        raise HTTPException(status_code=404, detail="Economic state not found")

    allowed, reason = state.check_hard_limit(request.limit_name, request.current_value)
    limit = state.hard_limits.get(request.limit_name, 0)

    return CheckLimitResponse(
        allowed=allowed,
        limit=limit,
        current=request.current_value,
        reason=reason
    )


@router.get("/{user_id}/check-feature/{feature_name}", response_model=CheckFeatureResponse)
async def check_feature(
    user_id: str,
    feature_name: str,
    session: AsyncSession = Depends(get_session),
):
    """
    Check if user can access a specific feature.
    
    Called by gateway/services before allowing feature access.
    """
    try:
        user_uuid = UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user_id format")

    result = await session.execute(
        select(UserEconomicState).where(UserEconomicState.user_id == user_uuid)
    )
    state = result.scalar_one_or_none()

    if not state:
        raise HTTPException(status_code=404, detail="Economic state not found")

    allowed = state.can_access_feature(feature_name)

    return CheckFeatureResponse(
        allowed=allowed,
        feature=feature_name,
        reason="" if allowed else f"Feature '{feature_name}' not available on {state.subscription_tier.value} tier"
    )


# ============================================
# SUBSCRIPTION MANAGEMENT ENDPOINTS
# ============================================

@router.post("/{user_id}/upgrade")
async def upgrade_subscription(
    user_id: str,
    new_tier: str,
    subscription_id: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
):
    """
    Upgrade user's subscription tier.
    
    Called after successful Stripe payment or admin action.
    """
    try:
        user_uuid = UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user_id format")

    result = await session.execute(
        select(UserEconomicState).where(UserEconomicState.user_id == user_uuid)
    )
    state = result.scalar_one_or_none()

    if not state:
        raise HTTPException(status_code=404, detail="Economic state not found")

    try:
        tier = SubscriptionTier(new_tier.lower())
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid tier: {new_tier}")

    state.upgrade_to_tier(tier)
    if subscription_id:
        state.subscription_id = subscription_id
        state.subscription_source = SubscriptionSource.STRIPE

    await session.commit()
    await session.refresh(state)

    return EconomicStateResponse(**state.to_dict())


@router.post("/{user_id}/add-credits")
async def add_credits(
    user_id: str,
    amount: int,
    reason: str = "credit_purchase",
    session: AsyncSession = Depends(get_session),
):
    """
    Add credits to user's balance.
    
    Called after successful credit purchase.
    """
    try:
        user_uuid = UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user_id format")

    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")

    # Use SELECT FOR UPDATE to maintain single-writer economic invariant
    result = await session.execute(
        select(UserEconomicState)
        .where(UserEconomicState.user_id == user_uuid)
        .with_for_update()  # Row-level lock
    )
    state = result.scalar_one_or_none()

    if not state:
        raise HTTPException(status_code=404, detail="Economic state not found")

    state.credit_balance += amount
    await session.commit()
    await session.refresh(state)

    return {
        "success": True,
        "new_balance": state.credit_balance,
        "amount_added": amount,
        "reason": reason
    }


@router.post("/{user_id}/set-status")
async def set_subscription_status(
    user_id: str,
    status: str,
    session: AsyncSession = Depends(get_session),
):
    """
    Set subscription status.
    
    Called by Stripe webhooks or admin actions.
    """
    try:
        user_uuid = UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user_id format")

    try:
        new_status = SubscriptionStatus(status.lower())
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

    result = await session.execute(
        select(UserEconomicState).where(UserEconomicState.user_id == user_uuid)
    )
    state = result.scalar_one_or_none()

    if not state:
        raise HTTPException(status_code=404, detail="Economic state not found")

    state.subscription_status = new_status
    await session.commit()
    await session.refresh(state)

    return EconomicStateResponse(**state.to_dict())
