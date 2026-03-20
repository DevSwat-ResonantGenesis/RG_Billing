"""Billing Service API routers."""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Request,
    Cookie,
)
from starlette.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

# Import crypto identity helper
try:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from shared.crypto_identity import get_crypto_identity
    CRYPTO_IDENTITY_AVAILABLE = True
except ImportError:
    CRYPTO_IDENTITY_AVAILABLE = False

from sqlalchemy import select

logger = logging.getLogger(__name__)

from .db import get_session
from .subscriptions import subscription_manager
from .credits import credit_manager
from .metering import usage_meter
from .invoices import invoice_manager
from .config import settings
from .models import CreditBalance

router = APIRouter(prefix="/billing", tags=["billing"])
dashboard_router = APIRouter(prefix="/dashboard", tags=["dashboard"])


# Request/Response models
class CreateSubscriptionRequest(BaseModel):
    plan: str
    billing_cycle: str = "monthly"
    payment_method_id: Optional[str] = None
    coupon_code: Optional[str] = None


class ChangePlanRequest(BaseModel):
    new_plan: str


class PurchaseCreditsRequest(BaseModel):
    amount_usd: float
    payment_method_id: Optional[str] = None


class DeductCreditsRequest(BaseModel):
    amount: int
    reference_type: str
    reference_id: Optional[str] = None
    description: Optional[str] = None


class RecordUsageRequest(BaseModel):
    usage_type: str
    quantity: int
    metadata: Optional[Dict[str, Any]] = None


class CreateInvoiceRequest(BaseModel):
    line_items: List[Dict[str, Any]]
    billing_info: Optional[Dict[str, Any]] = None
    due_days: int = 30


# Helper to get user ID
def get_user_id(x_user_id: str = Header(None), cookie_user_id: str = Cookie(None)) -> str:
    # Try header first, then cookie
    user_id = x_user_id or cookie_user_id
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID required")
    return user_id

# Helper to get user role
def get_user_role(x_user_role: str = Header(None)) -> str:
    return x_user_role or "user"

# Check if user is a platform developer/system/superuser (gets unlimited plan)
# Superusers (is_superuser=true) also get unlimited access
def is_dev_user(role: str, is_superuser: bool = False, unlimited_credits: bool = False) -> bool:
    return role in ["platform_dev", "system", "owner", "platform_owner"] or is_superuser or unlimited_credits

# Helper to get superuser status from header
def get_is_superuser(x_is_superuser: str = Header(None)) -> bool:
    return x_is_superuser == "true"

# Helper to get unlimited_credits flag from header
def get_unlimited_credits(x_unlimited_credits: str = Header(None)) -> bool:
    return (x_unlimited_credits or "").strip().lower() in ("true", "1", "yes")


async def _get_provider_usage(user_id: str, session: AsyncSession) -> List[Dict[str, Any]]:
    """Get provider usage breakdown for the current billing period."""
    from collections import defaultdict
    from .metering import usage_meter
    
    try:
        records = await usage_meter.get_usage_history(
            user_id=user_id,
            limit=1000,
            db_session=session,
        )
        
        provider_stats = defaultdict(lambda: {"requests": 0, "tokens": 0})
        for r in records:
            provider = r.extra_metadata.get("provider", "unknown") if r.extra_metadata else "unknown"
            if provider != "unknown":
                provider_stats[provider]["requests"] += 1
                provider_stats[provider]["tokens"] += r.quantity
        
        return [
            {"provider": provider, "requests": stats["requests"], "tokens": stats["tokens"]}
            for provider, stats in provider_stats.items()
        ]
    except Exception:
        return []


# Subscription endpoints
@router.get("/subscription")
async def get_subscription(
    user_id: str = Depends(get_user_id),
    user_role: str = Depends(get_user_role),
    is_superuser: bool = Depends(get_is_superuser),
    unlimited_credits: bool = Depends(get_unlimited_credits),
    session: AsyncSession = Depends(get_session),
):
    """Get current subscription."""
    # Dev users, superusers, and unlimited_credits users get unlimited plan
    if is_dev_user(user_role, is_superuser, unlimited_credits):
        return {"plan": "unlimited", "status": "active", "is_dev": True, "unlimited_credits": True}
    
    subscription = await subscription_manager.get_subscription(user_id, session)
    if not subscription:
        return {"plan": "developer", "status": "active"}
    return {
        "id": str(subscription.id),
        "plan": subscription.plan,
        "billing_cycle": subscription.billing_cycle,
        "status": subscription.status,
        "current_period_start": subscription.current_period_start.isoformat() if subscription.current_period_start else None,
        "current_period_end": subscription.current_period_end.isoformat() if subscription.current_period_end else None,
        "trial_end": subscription.trial_end.isoformat() if subscription.trial_end else None,
        "amount": float(subscription.amount) if subscription.amount else None,
        "currency": subscription.currency,
    }


@router.post("/subscription")
async def create_subscription(
    request: CreateSubscriptionRequest,
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Create or upgrade subscription."""
    try:
        subscription = await subscription_manager.create_subscription(
            user_id=user_id,
            plan=request.plan_id,
            billing_cycle=request.billing_cycle,
            payment_method_id=request.payment_method_id,
            coupon_code=request.coupon_code,
            db_session=session,
        )
        return {
            "status": "success",
            "subscription_id": str(subscription.id),
            "plan": subscription.plan,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/subscription/cancel")
async def cancel_subscription(
    at_period_end: bool = True,
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Cancel subscription."""
    try:
        subscription = await subscription_manager.cancel_subscription(
            user_id=user_id,
            at_period_end=at_period_end,
            db_session=session,
        )
        return {
            "status": "canceled",
            "effective_date": subscription.current_period_end.isoformat() if at_period_end and subscription.current_period_end else "immediate",
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/subscription/reactivate")
async def reactivate_subscription(
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Reactivate canceled subscription."""
    try:
        subscription = await subscription_manager.reactivate_subscription(user_id, session)
        return {"status": "reactivated", "plan": subscription.plan}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/subscription/change-plan")
async def change_plan(
    request: ChangePlanRequest,
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Change subscription plan."""
    try:
        subscription = await subscription_manager.change_plan(
            user_id=user_id,
            new_plan=request.new_plan,
            db_session=session,
        )
        return {"status": "changed", "new_plan": subscription.plan}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# Credit endpoints
@router.get("/credits")
async def get_credits(
    user_id: str = Depends(get_user_id),
    user_role: str = Depends(get_user_role),
    is_superuser: bool = Depends(get_is_superuser),
    unlimited_credits: bool = Depends(get_unlimited_credits),
    session: AsyncSession = Depends(get_session),
):
    """Get credit balance. Superusers and unlimited_credits users get unlimited credits."""
    # Superusers, dev users, and unlimited_credits users get unlimited credits
    if is_dev_user(user_role, is_superuser, unlimited_credits):
        return {
            "balance": 999999999,
            "unlimited": True,
            "lifetime_purchased": 0,
            "lifetime_used": 0,
            "lifetime_bonus": 0,
            "expiring_credits": 0,
            "expiration_date": None,
        }
    
    try:
        logger.info(f"Fetching credits for user: {user_id[:8]}...")
        balance_data = await credit_manager.get_balance(user_id, session)
        balance_data["unlimited"] = False
        logger.info(f"Successfully fetched credits for user: {user_id[:8]}...")
        return balance_data
    except Exception as e:
        logger.error(f"Error fetching credits for user {user_id[:8]}...: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch credit balance: {str(e)}"
        )



@router.get("/credits/balance/{target_user_id}")
async def get_credits_balance_by_id(
    target_user_id: str,
    x_user_role: str = Header(None),
    x_is_superuser: str = Header(None),
    x_unlimited_credits: str = Header(None),
    session: AsyncSession = Depends(get_session),
):
    """Get credit balance for a specific user (internal service use).
    
    This endpoint is used by other services (chat_service, ide_platform_service) to check
    if a user has credits remaining for platform key usage.
    
    Superusers and dev users get unlimited credits.
    Free tier users start with 1000 credits.
    """
    FREE_TIER_CREDITS = 1000
    
    # Check if target user is superuser/dev/unlimited_credits (passed via headers from calling service)
    is_superuser = x_is_superuser == "true"
    user_role = x_user_role or "user"
    _unlimited_credits = (x_unlimited_credits or "").strip().lower() in ("true", "1", "yes")
    
    if is_dev_user(user_role, is_superuser, _unlimited_credits):
        return {
            "user_id": target_user_id,
            "balance": 999999999,
            "free_tier_limit": FREE_TIER_CREDITS,
            "has_credits": True,
            "unlimited": True,
        }
    
    try:
        balance_data = await credit_manager.get_balance(target_user_id, session)
        return {
            "user_id": target_user_id,
            "balance": balance_data.get("balance", FREE_TIER_CREDITS),
            "free_tier_limit": FREE_TIER_CREDITS,
            "has_credits": balance_data.get("balance", FREE_TIER_CREDITS) > 0,
            "unlimited": False,
        }
    except Exception:
        # If user doesn't exist in billing yet, assume they have full free tier credits
        return {
            "user_id": target_user_id,
            "balance": FREE_TIER_CREDITS,
            "free_tier_limit": FREE_TIER_CREDITS,
            "has_credits": True,
            "unlimited": False,
            "note": "New user - full free tier credits"
        }



@router.post("/credits/purchase")
async def purchase_credits(
    request: PurchaseCreditsRequest,
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Purchase credits."""
    try:
        return await credit_manager.purchase_credits(
            user_id=user_id,
            amount_usd=request.amount_usd,
            payment_method_id=request.payment_method_id,
            db_session=session,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/credits/deduct")
async def deduct_credits(
    request: DeductCreditsRequest,
    user_id: str = Depends(get_user_id),
    user_role: str = Depends(get_user_role),
    is_superuser: bool = Depends(get_is_superuser),
    unlimited_credits: bool = Depends(get_unlimited_credits),
    session: AsyncSession = Depends(get_session),
):
    """Deduct credits (internal use)."""
    # Superusers, dev users, and unlimited_credits users have unlimited credits - skip deduction
    if is_dev_user(user_role, is_superuser, unlimited_credits):
        return {
            "status": "unlimited",
            "id": None,
            "transaction_id": None,
            "amount": 0,
            "balance_after": 999999999,
            "message": "Unlimited credits - no deduction needed"
        }
    
    try:
        transaction = await credit_manager.deduct_credits(
            user_id=user_id,
            amount=request.amount,
            reference_type=request.reference_type,
            reference_id=request.reference_id,
            description=request.description,
            db_session=session,
        )
        return {
            "status": "deducted",
            "id": str(transaction.id),
            "transaction_id": str(transaction.id),
            "amount": transaction.amount,
            "balance_after": transaction.balance_after,
        }
    except ValueError as e:
        plan = "developer"
        try:
            subscription = await subscription_manager.get_subscription(user_id, session)
            if subscription and getattr(subscription, "plan", None):
                plan = subscription.plan
            else:
                plan = "developer"
        except Exception:
            plan = "developer"

        plan_normalized = (plan or "developer").strip().lower()
        if plan_normalized in {"developer", "free"}:
            action_url = "/pricing"
            detail_msg = "Credits exhausted. Upgrade to Plus to get more credits."
        else:
            action_url = "/billing"
            detail_msg = "Credits exhausted. Buy more credits to continue."

        return JSONResponse(
            status_code=402,
            content={
                "error": "insufficient_credits",
                "detail": detail_msg,
                "message": detail_msg,
                "action_url": action_url,
                "required": request.amount,
            },
        )


@router.get("/credits/transactions")
async def get_credit_transactions(
    tx_type: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Get credit transaction history."""
    transactions = await credit_manager.get_transactions(
        user_id=user_id,
        tx_type=tx_type,
        limit=limit,
        offset=offset,
        db_session=session,
    )
    return [
        {
            "id": str(tx.id),
            "tx_type": tx.tx_type,
            "amount": tx.amount,
            "balance_after": tx.balance_after,
            "reference_type": tx.reference_type,
            "description": tx.description,
            "created_at": tx.created_at.isoformat(),
        }
        for tx in transactions
    ]


@router.post("/credits/bonus")
async def grant_bonus_credits(
    request: dict,
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Grant bonus credits to user (admin only in production)."""
    amount = request.get("amount", 0)
    reason = request.get("reason", "Bonus credits")
    expires_in_days = request.get("expires_in_days")
    
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    
    try:
        transaction = await credit_manager.grant_bonus_credits(
            user_id=user_id,
            amount=amount,
            reason=reason,
            expires_in_days=expires_in_days,
            db_session=session,
        )
        return {
            "status": "granted",
            "id": str(transaction.id),
            "amount": transaction.amount,
            "balance_after": transaction.balance_after,
            "expires_in_days": expires_in_days,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/credits/refund")
async def refund_credits(
    request: dict,
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Refund credits to user."""
    amount = request.get("amount", 0)
    original_tx_id = request.get("original_tx_id", "")
    reason = request.get("reason", "Credit refund")
    
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    
    try:
        transaction = await credit_manager.refund_credits(
            user_id=user_id,
            amount=amount,
            original_tx_id=original_tx_id,
            reason=reason,
            db_session=session,
        )
        return {
            "status": "refunded",
            "id": str(transaction.id),
            "amount": transaction.amount,
            "balance_after": transaction.balance_after,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# Usage metering endpoints
@router.post("/usage/record")
async def record_usage(
    request: RecordUsageRequest,
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Record usage."""
    record = await usage_meter.record_usage(
        user_id=user_id,
        usage_type=request.usage_type,
        quantity=request.quantity,
        metadata=request.metadata,
        db_session=session,
    )
    return {
        "status": "recorded",
        "record_id": str(record.id),
        "usage_type": record.usage_type,
        "quantity": record.quantity,
    }


@router.get("/usage/summary")
async def get_usage_summary(
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Get usage summary for current period."""
    return await usage_meter.get_usage_summary(user_id=user_id, db_session=session)


@router.get("/usage/metrics")
async def get_usage_metrics(
    user_id: str = Depends(get_user_id),
    user_role: str = Depends(get_user_role),
    is_superuser: bool = Depends(get_is_superuser),
    unlimited_credits: bool = Depends(get_unlimited_credits),
    session: AsyncSession = Depends(get_session),
):
    """Get full usage metrics - compatibility with frontend UsageMetrics interface."""
    import httpx
    
    # Get usage summary from billing records
    summary = await usage_meter.get_usage_summary(user_id=user_id, db_session=session)
    usage_data = summary.get("usage", {})
    
    # Extract token usage from summary
    tokens_used = usage_data.get("tokens", {}).get("quantity", 0)
    if tokens_used == 0:
        # Also check llm_tokens usage type
        tokens_used = usage_data.get("llm_tokens", {}).get("quantity", 0)
    
    # Get subscription first (needed for billing info)
    subscription = await subscription_manager.get_subscription(user_id, session)
    
    # Dev users get unlimited plan
    if is_dev_user(user_role, is_superuser, unlimited_credits):
        plan = "unlimited"
    else:
        plan = subscription.plan if subscription else "developer"
    
    # Plan limits matching frontend PLAN_LIMITS - 3 tiers: developer, plus, enterprise
    plan_limits = {
        "unlimited": {"tokens": -1, "agents": -1, "teams": -1, "memory": -1, "users": -1, "conversations": -1, "credits": -1},
        "enterprise": {"tokens": -1, "agents": -1, "teams": -1, "memory": -1, "users": -1, "conversations": -1, "credits": -1},
        "plus": {"tokens": 5000000, "agents": 20, "teams": 5, "memory": 100, "users": 5, "conversations": 1000, "credits": 75000},
        "professional": {"tokens": 5000000, "agents": 20, "teams": 5, "memory": 100, "users": 5, "conversations": 1000, "credits": 75000},  # Legacy alias -> plus
        "pro": {"tokens": 5000000, "agents": 20, "teams": 5, "memory": 100, "users": 5, "conversations": 1000, "credits": 75000},  # Legacy alias -> plus
        "developer": {"tokens": 100000, "agents": 3, "teams": 0, "memory": 5, "users": 1, "conversations": 1000, "credits": 1000},
        "free": {"tokens": 100000, "agents": 3, "teams": 0, "memory": 5, "users": 1, "conversations": 1000, "credits": 1000},  # Legacy alias -> developer
    }
    limits = plan_limits.get(plan, plan_limits["developer"])
    
    tokens_limit = limits["tokens"]
    agents_limit = limits["agents"]
    teams_limit = limits["teams"]
    memory_limit = limits["memory"]
    users_limit = limits["users"]
    conversations_limit = limits["conversations"]
    
    # Fetch real agent count from agent_engine_service
    agents_active = 0
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://agent_engine_service:8004/agents",
                headers={"x-user-id": user_id},
                timeout=5.0
            )
            if resp.status_code == 200:
                agents_data = resp.json()
                if isinstance(agents_data, list):
                    agents_active = len(agents_data)
                elif isinstance(agents_data, dict) and "agents" in agents_data:
                    agents_active = len(agents_data["agents"])
    except Exception as e:
        logger.warning(f"Failed to fetch agents: {e}")
    
    # Fetch real conversation count from chat_service
    conversations_count = 0
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://chat_service:8002/resonant-chat/conversations",
                headers={"x-user-id": user_id},
                timeout=5.0
            )
            if resp.status_code == 200:
                conv_data = resp.json()
                if isinstance(conv_data, list):
                    conversations_count = len(conv_data)
                elif isinstance(conv_data, dict) and "conversations" in conv_data:
                    conversations_count = len(conv_data["conversations"])
    except Exception as e:
        logger.warning(f"Failed to fetch conversations: {e}")
    
    # Fetch memory anchors and RAG documents from memory_service
    memory_anchors = 0
    storage_used_mb = 0
    rag_documents = 0
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://memory_service:8003/memory/stats",
                headers={"x-user-id": user_id},
                timeout=5.0
            )
            if resp.status_code == 200:
                mem_data = resp.json()
                memory_anchors = mem_data.get("anchors_count", 0)
                storage_used_mb = mem_data.get("storage_mb", 0)
                rag_documents = mem_data.get("rag_documents", 0)
    except Exception as e:
        logger.warning(f"Failed to fetch memory stats: {e}")
    
    # Fetch compute hours from IDE service
    compute_hours_used = 0
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://ide_platform_service:8005/api/ide/usage",
                headers={"x-user-id": user_id},
                timeout=5.0
            )
            if resp.status_code == 200:
                ide_data = resp.json()
                compute_hours_used = ide_data.get("compute_hours", 0)
    except Exception as e:
        logger.warning(f"Failed to fetch IDE usage: {e}")
    
    # Get credit balance
    credits_balance = 0
    credits_used = 0
    try:
        result = await session.execute(
            select(CreditBalance).where(CreditBalance.user_id == user_id)
        )
        credit_record = result.scalar_one_or_none()
        if credit_record:
            credits_balance = credit_record.balance
            credits_used = credit_record.lifetime_used  # Fixed: was lifetime_spent, should be lifetime_used
    except Exception as e:
        logger.warning(f"Failed to fetch credits: {e}")
    
    # Build metrics response matching frontend interface
    return {
        "tokens": {
            "used": tokens_used,
            "limit": tokens_limit,
            "remaining": tokens_limit - tokens_used if tokens_limit > 0 else -1,
            "percentUsed": (tokens_used / max(tokens_limit, 1)) * 100 if tokens_limit > 0 else 0,
        },
        "agents": {
            "active": agents_active,
            "limit": agents_limit,
            "remaining": agents_limit - agents_active if agents_limit > 0 else -1,
        },
        "teams": {
            "created": usage_data.get("teams", {}).get("quantity", 0),
            "limit": teams_limit,
            "remaining": teams_limit if teams_limit > 0 else -1,
        },
        "memory": {
            "anchorsUsed": memory_anchors,
            "anchorsLimit": memory_limit,
            "storageUsedMB": storage_used_mb,
            "storageLimitMB": 5000 if plan in ["plus", "professional", "pro"] else 100,
        },
        "ragDocuments": {
            "used": rag_documents,
            "limit": 5 if plan in ["developer", "free"] else (100 if plan in ["plus", "professional", "pro"] else -1),
        },
        "computeHours": {
            "used": compute_hours_used,
            "limit": 10 if plan in ["developer", "free"] else (100 if plan in ["plus", "professional", "pro"] else -1),
        },
        "providers": {
            "available": ["openai", "anthropic", "gemini", "groq"],
            "used": await _get_provider_usage(user_id, session),
        },
        "users": {
            "active": 1,
            "limit": users_limit,
        },
        "conversations": {
            "count": conversations_count,
            "limit": conversations_limit,
        },
        "credits": {
            "balance": credits_balance,
            "used": credits_used,
            "limit": limits["credits"],
        },
        "billing": {
            "planId": plan if plan != "free" else "developer",
            "planName": "Developer" if plan in ["free", "developer"] else plan.replace("_", " ").title(),
            "billingPeriod": subscription.billing_cycle if subscription else "monthly",
            "currentPeriodStart": subscription.current_period_start.isoformat() if subscription and subscription.current_period_start else "",
            "currentPeriodEnd": subscription.current_period_end.isoformat() if subscription and subscription.current_period_end else "",
            "nextBillingDate": subscription.current_period_end.isoformat() if subscription and subscription.current_period_end else "",
        },
    }


@router.get("/usage/tokens/history")
async def get_token_history(
    days: int = 30,
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Get token usage history for charts."""
    records = await usage_meter.get_usage_history(
        user_id=user_id,
        usage_type="tokens",
        limit=days,
        db_session=session,
    )
    
    # Group by date
    from collections import defaultdict
    daily_usage = defaultdict(int)
    for r in records:
        date_str = r.created_at.strftime("%Y-%m-%d")
        daily_usage[date_str] += r.quantity
    
    return [
        {"date": date, "tokens": tokens}
        for date, tokens in sorted(daily_usage.items())
    ]


@router.get("/usage/providers")
async def get_provider_usage(
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Get provider usage breakdown."""
    records = await usage_meter.get_usage_history(
        user_id=user_id,
        limit=1000,
        db_session=session,
    )
    
    # Group by provider
    from collections import defaultdict
    provider_stats = defaultdict(lambda: {"requests": 0, "tokens": 0, "cost": 0.0})
    
    for r in records:
        provider = r.extra_metadata.get("provider", "unknown") if r.extra_metadata else "unknown"
        provider_stats[provider]["requests"] += 1
        provider_stats[provider]["tokens"] += r.quantity
        provider_stats[provider]["cost"] += float(r.total_cost) if r.total_cost else 0.0
    
    return [
        {
            "provider": provider,
            "requests": stats["requests"],
            "tokens": stats["tokens"],
            "cost": stats["cost"],
        }
        for provider, stats in provider_stats.items()
    ]


@router.get("/usage/activity")
async def get_usage_activity(
    days: int = 30,
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Get usage activity for dashboard - daily breakdown."""
    from datetime import datetime, timedelta
    from sqlalchemy import func
    from .models import UsageRecord
    
    cutoff = datetime.utcnow() - timedelta(days=days)
    
    # Get daily usage grouped by date
    result = await session.execute(
        select(
            func.date(UsageRecord.created_at).label('date'),
            func.sum(UsageRecord.quantity).label('total_usage'),
            func.count(UsageRecord.id).label('request_count')
        )
        .where(UsageRecord.user_id == user_id)
        .where(UsageRecord.created_at >= cutoff)
        .group_by(func.date(UsageRecord.created_at))
        .order_by(func.date(UsageRecord.created_at).desc())
    )
    
    rows = result.fetchall()
    
    return [
        {
            "date": str(row[0]),
            "usage": row[1] or 0,
            "requests": row[2] or 0,
        }
        for row in rows
    ]


@router.get("/usage/history")
async def get_usage_history(
    usage_type: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Get usage history."""
    records = await usage_meter.get_usage_history(
        user_id=user_id,
        usage_type=usage_type,
        limit=limit,
        offset=offset,
        db_session=session,
    )
    return [
        {
            "id": str(r.id),
            "usage_type": r.usage_type,
            "quantity": r.quantity,
            "unit_price": float(r.unit_price) if r.unit_price else None,
            "total_cost": float(r.total_cost) if r.total_cost else None,
            "created_at": r.created_at.isoformat(),
        }
        for r in records
    ]


@router.get("/usage/limits")
async def check_usage_limits(
    usage_type: str,
    quantity: int = 1,
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Check usage limits."""
    return await usage_meter.check_usage_limits(
        user_id=user_id,
        usage_type=usage_type,
        requested_quantity=quantity,
        db_session=session,
    )


@router.get("/usage/export")
async def export_usage_csv(
    days: int = 30,
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Export usage data as CSV."""
    from fastapi.responses import StreamingResponse
    import io
    import csv
    
    records = await usage_meter.get_usage_history(
        user_id=user_id,
        limit=10000,
        db_session=session,
    )
    
    # Filter by date range
    from datetime import datetime, timedelta
    cutoff_date = datetime.utcnow() - timedelta(days=days)
    records = [r for r in records if r.created_at >= cutoff_date]
    
    # Create CSV
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Header
    writer.writerow([
        "Date", "Time", "Type", "Quantity", "Unit Price", "Total Cost", 
        "Provider", "Description"
    ])
    
    # Data rows
    for r in records:
        provider = r.extra_metadata.get("provider", "") if r.extra_metadata else ""
        description = r.extra_metadata.get("description", "") if r.extra_metadata else ""
        writer.writerow([
            r.created_at.strftime("%Y-%m-%d"),
            r.created_at.strftime("%H:%M:%S"),
            r.usage_type,
            r.quantity,
            float(r.unit_price) if r.unit_price else 0,
            float(r.total_cost) if r.total_cost else 0,
            provider,
            description,
        ])
    
    output.seek(0)
    
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=usage_export_{datetime.utcnow().strftime('%Y%m%d')}.csv"
        }
    )


# Invoice endpoints
@router.get("/invoices")
async def list_invoices(
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """List invoices."""
    invoices = await invoice_manager.list_invoices(
        user_id=user_id,
        status=status,
        limit=limit,
        offset=offset,
        db_session=session,
    )
    return [
        {
            "id": str(inv.id),
            "invoice_number": inv.invoice_number,
            "status": inv.status,
            "total": float(inv.total),
            "amount_due": float(inv.amount_due),
            "currency": inv.currency,
            "due_date": inv.due_date.isoformat() if inv.due_date else None,
            "pdf_url": inv.stripe_invoice_pdf,
            "hosted_url": inv.stripe_hosted_invoice_url,
            "created_at": inv.created_at.isoformat(),
        }
        for inv in invoices
    ]


@router.get("/invoices/{invoice_id}")
async def get_invoice(
    invoice_id: str,
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Get invoice details."""
    invoice = await invoice_manager.get_invoice(invoice_id, session)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if str(invoice.user_id) != user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    return {
        "id": str(invoice.id),
        "invoice_number": invoice.invoice_number,
        "status": invoice.status,
        "subtotal": float(invoice.subtotal),
        "tax": float(invoice.tax),
        "total": float(invoice.total),
        "amount_paid": float(invoice.amount_paid),
        "amount_due": float(invoice.amount_due),
        "currency": invoice.currency,
        "line_items": invoice.line_items,
        "billing_name": invoice.billing_name,
        "billing_email": invoice.billing_email,
        "billing_address": invoice.billing_address,
        "period_start": invoice.period_start.isoformat() if invoice.period_start else None,
        "period_end": invoice.period_end.isoformat() if invoice.period_end else None,
        "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
        "paid_at": invoice.paid_at.isoformat() if invoice.paid_at else None,
        "pdf_url": invoice.stripe_invoice_pdf,
        "hosted_url": invoice.stripe_hosted_invoice_url,
        "created_at": invoice.created_at.isoformat(),
    }


@router.get("/invoices/stats")
async def get_invoice_stats(
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Get invoice statistics."""
    return await invoice_manager.get_invoice_stats(user_id, session)


# Webhook endpoint
@router.post("/webhook/stripe")
async def stripe_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Handle Stripe webhooks."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        import stripe
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Webhook error: {str(e)}")

    result = await subscription_manager.handle_webhook(
        event_type=event["type"],
        event_data=event["data"],
        db_session=session,
    )

    return result


# ============================================
# COMPATIBILITY ENDPOINTS
# Frontend expects these paths from old backend
# ============================================

@router.get("/overview")
async def billing_overview(
    user_id: str = Depends(get_user_id),
    user_role: str = Depends(get_user_role),
    is_superuser: bool = Depends(get_is_superuser),
    unlimited_credits: bool = Depends(get_unlimited_credits),
    session: AsyncSession = Depends(get_session),
):
    """Get billing overview - compatibility with old backend.
    
    Returns combined subscription, usage, invoices, and payment methods.
    """
    # Get subscription first
    subscription = await subscription_manager.get_subscription(user_id, session)
    
    # Dev users get unlimited plan
    if is_dev_user(user_role, is_superuser, unlimited_credits):
        plan = "unlimited"
        status = "active"
    else:
        plan = subscription.plan if subscription else "free"
        status = subscription.status if subscription else "active"
    
    # Get usage summary
    usage = await usage_meter.get_usage_summary(user_id=user_id, db_session=session)
    
    # Get recent invoices
    invoices_list = await invoice_manager.list_invoices(
        user_id=user_id,
        limit=5,
        db_session=session,
    )
    
    # Build overview response matching old backend format
    overview = {
        "plan": plan,
        "status": status,
    }
    
    if subscription:
        overview.update({
            "period_start": subscription.current_period_start.isoformat() if subscription.current_period_start else None,
            "period_end": subscription.current_period_end.isoformat() if subscription.current_period_end else None,
            "amount_due_cents": int(subscription.amount * 100) if subscription.amount else 0,
            "currency": subscription.currency or "usd",
            "billing_status": subscription.status,
        })
    
    invoices = [
        {
            "period_start": inv.period_start.isoformat() if inv.period_start else None,
            "period_end": inv.period_end.isoformat() if inv.period_end else None,
            "amount_due_cents": int(inv.amount_due * 100) if inv.amount_due else 0,
            "currency": inv.currency,
            "status": inv.status,
            "invoice_url": inv.stripe_hosted_invoice_url,
        }
        for inv in invoices_list
    ]
    
    # Payment methods placeholder (would need Stripe integration)
    payment_methods = []
    
    return {
        "overview": overview,
        "usage": usage,
        "invoices": invoices,
        "payment_methods": payment_methods,
        "plan": plan,
    }


@router.post("/change-plan")
async def change_plan_compat(
    request: ChangePlanRequest,
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Change plan - compatibility with old backend path."""
    try:
        subscription = await subscription_manager.change_plan(
            user_id=user_id,
            new_plan=request.new_plan,
            db_session=session,
        )
        return {"success": True, "plan": subscription.plan}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/payment-methods")
async def get_payment_methods(
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Get payment methods for user."""
    # In production, this would fetch from Stripe
    return []


@router.post("/payment-methods")
async def add_payment_method_compat(
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Add payment method - compatibility with old backend.
    
    Returns redirect URL to Stripe payment setup.
    """
    # In production, this would create a Stripe SetupIntent
    return {"redirect_url": "https://billing.resonantgraph.com/payment"}


@router.delete("/payment-methods/{pm_id}")
async def delete_payment_method_compat(
    pm_id: str,
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Delete payment method - compatibility with old backend."""
    # In production, this would delete from Stripe
    return {"success": True}


@router.post("/payment-methods/{pm_id}/default")
async def set_default_payment_method(
    pm_id: str,
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Set default payment method."""
    # In production, this would update Stripe customer default
    return {"success": True, "default_payment_method": pm_id}


@router.get("/invoices/{invoice_id}/pdf")
async def get_invoice_pdf(
    invoice_id: str,
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Get invoice PDF URL."""
    invoice = await invoice_manager.get_invoice(invoice_id, session)
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if str(invoice.user_id) != user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    if invoice.stripe_invoice_pdf:
        return {"url": invoice.stripe_invoice_pdf}
    raise HTTPException(status_code=404, detail="PDF not available")


@router.post("/portal")
async def create_portal_session(
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Create Stripe billing portal session."""
    # In production, this would create a Stripe BillingPortal session
    return {"url": "https://billing.stripe.com/portal"}


# ============================================
# OWNER DASHBOARD STATS ENDPOINTS
# ============================================

@router.get("/subscriptions/stats")
async def get_subscription_stats(
    session: AsyncSession = Depends(get_session),
):
    """Get subscription statistics for owner dashboard."""
    from sqlalchemy import select, func
    from .models import Subscription
    from decimal import Decimal
    
    # Get MRR (Monthly Recurring Revenue) from active subscriptions
    mrr_result = await session.execute(
        select(func.sum(Subscription.amount))
        .where(Subscription.status == "active")
        .where(Subscription.billing_cycle == "monthly")
    )
    monthly_mrr = mrr_result.scalar() or Decimal(0)
    
    # Get yearly subscriptions (divide by 12 for MRR)
    yearly_result = await session.execute(
        select(func.sum(Subscription.amount))
        .where(Subscription.status == "active")
        .where(Subscription.billing_cycle == "yearly")
    )
    yearly_amount = yearly_result.scalar() or Decimal(0)
    yearly_mrr = yearly_amount / 12 if yearly_amount else Decimal(0)
    
    total_mrr = float(monthly_mrr + yearly_mrr)
    
    # Total subscription revenue = only report actual MRR, not projected annual
    # Real lifetime revenue should come from Stripe payment records
    total_revenue = total_mrr  # Current MRR only — no fake annual projections
    
    # Get subscription counts by plan
    plan_counts_result = await session.execute(
        select(Subscription.plan, func.count(Subscription.id))
        .where(Subscription.status == "active")
        .group_by(Subscription.plan)
    )
    plan_counts = {row[0]: row[1] for row in plan_counts_result.fetchall()}
    
    return {
        "mrr": total_mrr,
        "total_revenue": total_revenue,
        "active_subscriptions": sum(plan_counts.values()),
        "plan_breakdown": plan_counts,
    }


@router.get("/credits/stats")
async def get_credits_stats(
    session: AsyncSession = Depends(get_session),
):
    """Get credit statistics for owner dashboard."""
    from sqlalchemy import select, func
    from .models import CreditBalance, CreditTransaction
    
    # Get total credits consumed (lifetime)
    consumed_result = await session.execute(
        select(func.sum(CreditBalance.lifetime_used))
    )
    total_consumed = consumed_result.scalar() or 0
    
    # Get total credits purchased
    purchased_result = await session.execute(
        select(func.sum(CreditBalance.lifetime_purchased))
    )
    total_purchased = purchased_result.scalar() or 0
    
    # Get current total balance
    balance_result = await session.execute(
        select(func.sum(CreditBalance.balance))
    )
    total_balance = balance_result.scalar() or 0
    
    return {
        "total_consumed": total_consumed,
        "total_purchased": total_purchased,
        "total_balance": total_balance,
    }


@router.get("/usage/breakdown")
async def get_usage_breakdown(
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Get credit usage breakdown by category for the current billing period."""
    data = await _get_breakdown_data(user_id, session)
    by_service = (data or {}).get("breakdown") or {}
    return {
        **by_service,
        "total": data.get("total"),
        "period_start": data.get("period_start"),
        "period_end": data.get("period_end"),
    }


# ============================================
# DASHBOARD ENDPOINTS (on /billing prefix)
# ============================================

@router.get("/dashboard/me")
async def get_dashboard_me(
    user_id: str = Depends(get_user_id),
    user_role: str = Depends(get_user_role),
    is_superuser: bool = Depends(get_is_superuser),
    unlimited_credits: bool = Depends(get_unlimited_credits),
    session: AsyncSession = Depends(get_session),
):
    """Get dashboard data for current user - combines subscription, credits, and usage."""
    return await _get_dashboard_data(user_id, user_role, session, is_superuser=is_superuser, unlimited_credits=unlimited_credits)


@router.get("/dashboard/me/breakdown")
async def get_dashboard_breakdown(
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Get credit usage breakdown for dashboard charts."""
    return await _get_breakdown_data(user_id, session)


# ============================================
# DASHBOARD ENDPOINTS (on /dashboard prefix for gateway compatibility)
# ============================================

@dashboard_router.get("/me")
async def dashboard_me(
    user_id: str = Depends(get_user_id),
    user_role: str = Depends(get_user_role),
    is_superuser: bool = Depends(get_is_superuser),
    unlimited_credits: bool = Depends(get_unlimited_credits),
    session: AsyncSession = Depends(get_session),
):
    """Get dashboard data for current user."""
    return await _get_dashboard_data(user_id, user_role, session, is_superuser=is_superuser, unlimited_credits=unlimited_credits)


@dashboard_router.get("/me/breakdown")
async def dashboard_breakdown(
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Get credit usage breakdown."""
    return await _get_breakdown_data(user_id, session)


@dashboard_router.get("/{user_id_param}")
async def dashboard_by_user(
    user_id_param: str,
    user_id: str = Depends(get_user_id),
    user_role: str = Depends(get_user_role),
    is_superuser: bool = Depends(get_is_superuser),
    unlimited_credits: bool = Depends(get_unlimited_credits),
    session: AsyncSession = Depends(get_session),
):
    """Get dashboard data for specific user (uses requesting user's context)."""
    return await _get_dashboard_data(user_id, user_role, session, is_superuser=is_superuser, unlimited_credits=unlimited_credits)


@dashboard_router.get("/{user_id_param}/breakdown")
async def dashboard_breakdown_by_user(
    user_id_param: str,
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Get breakdown for specific user."""
    return await _get_breakdown_data(user_id, session)


# Helper functions for dashboard data
async def _get_dashboard_data(user_id: str, user_role: str, session: AsyncSession, *, is_superuser: bool = False, unlimited_credits: bool = False):
    """Internal helper to get dashboard data with Redis caching for scale."""
    from .cache import get_cache
    
    cache = get_cache()
    
    # Try cache first (30s TTL for dashboard data)
    cached_data = await cache.get_dashboard(user_id)
    if cached_data is not None:
        logger.debug(f"Cache hit for dashboard: {user_id}")
        return cached_data
    
    # Cache miss - fetch from database
    subscription = await subscription_manager.get_subscription(user_id, session)
    
    if is_dev_user(user_role, is_superuser, unlimited_credits):
        plan = "unlimited"
        status = "active"
    else:
        plan = subscription.plan if subscription else "developer"
        status = subscription.status if subscription else "active"
    
    balance_data = await credit_manager.get_balance(user_id, session)
    usage = await usage_meter.get_usage_summary(user_id=user_id, db_session=session)
    
    # Calculate days remaining in billing period
    from datetime import datetime
    import calendar
    now = datetime.utcnow()
    
    if subscription and subscription.current_period_end:
        delta = subscription.current_period_end - now
        days_remaining = max(0, delta.days)
    else:
        # For developer tier: days until end of current month
        _, days_in_month = calendar.monthrange(now.year, now.month)
        days_remaining = days_in_month - now.day
    
    # Get tier credits limit from pricing config
    from .pricing_loader import get_plan_credits
    tier_credits = get_plan_credits(plan.lower() if plan else "developer")
    
    # Calculate usage this period
    usage_this_period = usage.get("usage", {}).get("tokens", {}).get("quantity", 0) if usage else 0
    
    # Calculate burn rate (credits per day)
    _, days_in_month = calendar.monthrange(now.year, now.month)
    days_elapsed = now.day
    burn_rate = round(usage_this_period / days_elapsed) if days_elapsed > 0 and usage_this_period > 0 else 0
    
    # Get activity counts from usage data
    usage_by_type = usage.get("usage", {}) if usage else {}
    messages_count = usage_by_type.get("chat", {}).get("count", 0)
    sessions_count = usage_by_type.get("sessions", {}).get("count", 0)
    memories_count = usage_by_type.get("memory", {}).get("count", 0)
    
    result = {
        "subscription": {
            "plan": plan,
            "status": status,
            "billing_cycle": subscription.billing_cycle if subscription else "monthly",
            "current_period_start": subscription.current_period_start.isoformat() if subscription and subscription.current_period_start else None,
            "current_period_end": subscription.current_period_end.isoformat() if subscription and subscription.current_period_end else None,
        },
        "credits": balance_data,
        "usage": usage,
        # Frontend-expected fields
        "current_balance": balance_data.get("balance", 0) if balance_data else 0,
        "tier_credits": tier_credits,
        "usage_this_period": usage_this_period,
        "days_remaining": days_remaining,
        "burn_rate": burn_rate,
        "agents": 0,
        "agents_limit": -1,  # Unlimited - we bill by credits only
        "messages": messages_count,
        "memories": memories_count,
        "sessions": sessions_count,
        "recent_transactions": [],  # Will be populated below
    }
    
    # Get recent transactions for activity feed
    from .models import CreditTransaction
    from sqlalchemy import select
    
    recent_tx_query = await session.execute(
        select(CreditTransaction)
        .where(CreditTransaction.user_id == user_id)
        .order_by(CreditTransaction.created_at.desc())
        .limit(10)
    )
    recent_txs = recent_tx_query.scalars().all()
    
    result["recent_transactions"] = [
        {
            "tx_type": tx.tx_type,
            "amount": tx.amount,
            "description": tx.description or f"{tx.tx_type}: {abs(tx.amount)} credits",
            "reference_type": tx.reference_type,
            "created_at": tx.created_at.isoformat() if tx.created_at else None,
        }
        for tx in recent_txs
    ]
    
    # Cache the result
    await cache.set_dashboard(user_id, result)
    logger.debug(f"Cached dashboard data for: {user_id}")
    
    return result


async def _get_breakdown_data(user_id: str, session: AsyncSession):
    """Internal helper to get breakdown data with Redis caching for scale."""
    from .cache import get_cache
    from sqlalchemy import select, func
    from datetime import datetime
    from .models import CreditTransaction
    
    cache = get_cache()
    
    # Try cache first (60s TTL for breakdown data)
    cached_data = await cache.get_breakdown(user_id)
    if cached_data is not None:
        logger.debug(f"Cache hit for breakdown: {user_id}")
        return cached_data
    
    now = datetime.utcnow()
    period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    result = await session.execute(
        select(
            CreditTransaction.reference_type,
            func.sum(CreditTransaction.amount)
        )
        .where(CreditTransaction.user_id == user_id)
        .where(CreditTransaction.created_at >= period_start)
        .where(CreditTransaction.amount < 0)
        .group_by(CreditTransaction.reference_type)
    )
    
    breakdown = {row[0] or "other": abs(row[1]) for row in result.fetchall()}

    def _sum_prefix(prefix: str) -> int:
        return sum(v for k, v in breakdown.items() if isinstance(k, str) and k.startswith(prefix))

    chat_credits = (
        breakdown.get("chat", 0)
        + breakdown.get("llm_tokens", 0)
        + breakdown.get("chat_message", 0)
        + breakdown.get("llm", 0)
        + breakdown.get("message", 0)
    )

    agent_credits = (
        breakdown.get("agent", 0)
        + breakdown.get("agent_run", 0)
        + breakdown.get("agent_step", 0)
        + breakdown.get("agent_session", 0)
        + breakdown.get("agent_session_start", 0)
        + breakdown.get("agent_tool", 0)
        + _sum_prefix("agent_")
    )

    compute_credits = (
        breakdown.get("compute", 0)
        + breakdown.get("code_execution", 0)
        + breakdown.get("terminal_session", 0)
        + breakdown.get("terminal", 0)
        + breakdown.get("preview", 0)
        + breakdown.get("preview_session", 0)
    )

    workflow_credits = (
        breakdown.get("workflow", 0)
        + breakdown.get("workflow_step", 0)
        + breakdown.get("workflow_run", 0)
        + _sum_prefix("workflow_")
    )

    storage_credits = (
        breakdown.get("storage", 0)
        + breakdown.get("memory", 0)
        + breakdown.get("memory_write", 0)
        + breakdown.get("rag", 0)
        + breakdown.get("rag_upload", 0)
        + _sum_prefix("memory_")
        + _sum_prefix("rag_")
    )

    code_visualizer_credits = (
        breakdown.get("code_visualizer", 0)
        + breakdown.get("code_visualizer_analysis", 0)
        + breakdown.get("code_visualizer_governance", 0)
        + breakdown.get("code_analysis", 0)
        + breakdown.get("codebase_analysis", 0)
        + breakdown.get("governance_check", 0)
        + breakdown.get("graph_export", 0)
        + _sum_prefix("code_visualizer_")
    )
    
    total = sum(breakdown.values())
    known_total = chat_credits + agent_credits + compute_credits + workflow_credits + storage_credits + code_visualizer_credits
    other_credits = max(0, total - known_total)
    
    result_data = {
        "breakdown": {
            "chat": chat_credits,
            "agents": agent_credits,
            "compute": compute_credits,
            "workflows": workflow_credits,
            "storage": storage_credits,
            "code_visualizer": code_visualizer_credits,
            "other": other_credits,
        },
        "total": total,
        "period_start": period_start.isoformat(),
        "period_end": now.isoformat(),
    }
    
    # Cache the result
    await cache.set_breakdown(user_id, result_data)
    logger.debug(f"Cached breakdown data for: {user_id}")
    
    return result_data


# ============================================
# CHECKOUT ENDPOINTS
# ============================================

class CreateCheckoutSessionRequest(BaseModel):
    plan_id: str  # Changed from 'plan' to 'plan_id' to match frontend
    billing_cycle: str = "monthly"
    success_url: Optional[str] = None
    cancel_url: Optional[str] = None


class StripeCheckoutRequest(BaseModel):
    price_id: str
    success_url: str
    cancel_url: str


@router.post("/stripe/checkout")
async def create_stripe_checkout(
    request: StripeCheckoutRequest,
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Create Stripe checkout session for any price ID (API subscriptions, etc)."""
    try:
        import stripe
        stripe.api_key = settings.STRIPE_SECRET_KEY
        
        if not stripe.api_key:
            raise HTTPException(status_code=500, detail="Stripe not configured")
        
        # Determine mode based on price type
        # For now, assume subscription mode for recurring prices
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price': request.price_id,
                'quantity': 1,
            }],
            mode='subscription',
            success_url=request.success_url,
            cancel_url=request.cancel_url,
            metadata={
                'user_id': user_id,
                'price_id': request.price_id,
            },
        )
        
        return {
            "session_id": checkout_session.id,
            "url": checkout_session.url,
        }
        
    except Exception as e:
        logger.error(f"Stripe checkout session creation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/checkout/subscription")
async def create_checkout_session(
    request: CreateCheckoutSessionRequest,
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Create Stripe checkout session for subscription."""
    try:
        import stripe
        stripe.api_key = settings.STRIPE_SECRET_KEY
        
        if not stripe.api_key:
            raise HTTPException(status_code=500, detail="Stripe not configured")
        
        # Map plan to price ID
        from .stripe_integration import get_price_id_for_tier, SubscriptionTier
        
        tier_map = {
            "developer": SubscriptionTier.DEVELOPER,
            "plus": SubscriptionTier.PLUS,
            "enterprise": SubscriptionTier.ENTERPRISE,
            # API Subscriptions
            "state_physics_dev": SubscriptionTier.STATE_PHYSICS_DEV,
            "state_physics_startup": SubscriptionTier.STATE_PHYSICS_STARTUP,
            "hash_sphere_memory_dev": SubscriptionTier.HASH_SPHERE_DEV,
            "hash_sphere_memory_startup": SubscriptionTier.HASH_SPHERE_STARTUP,
        }
        
        tier = tier_map.get(request.plan_id.lower(), SubscriptionTier.DEVELOPER)
        price_id = get_price_id_for_tier(tier, request.billing_cycle)
        
        if not price_id:
            if tier == SubscriptionTier.DEVELOPER:
                # Free tier - no checkout needed
                return {"status": "success", "message": "Free tier activated"}
            else:
                raise HTTPException(status_code=400, detail=f"No price configured for {request.plan_id}")
        
        # Create checkout session
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price': price_id,
                'quantity': 1,
            }],
            mode='subscription',
            success_url=request.success_url or f'{settings.FRONTEND_URL}/billing?success=true',
            cancel_url=request.cancel_url or f'{settings.FRONTEND_URL}/billing?canceled=true',
            metadata={
                'user_id': user_id,
                'plan': request.plan_id,
                'billing_cycle': request.billing_cycle,
            },
        )
        
        return {
            "status": "success",
            "checkout_url": checkout_session.url,
            "session_id": checkout_session.id,
        }
        
    except Exception as e:
        logger.error(f"Checkout session creation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/checkout/credits")
async def create_credit_checkout_session(
    request: PurchaseCreditsRequest,
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Create Stripe checkout session for credit purchase."""
    try:
        import stripe
        stripe.api_key = settings.STRIPE_SECRET_KEY
        
        if not stripe.api_key:
            raise HTTPException(status_code=500, detail="Stripe not configured")
        
        # Calculate credits and amount
        credits = int(request.amount_usd * settings.CREDITS_PER_DOLLAR)
        
        if request.amount_usd < (settings.MIN_CREDIT_PURCHASE / settings.CREDITS_PER_DOLLAR):
            raise HTTPException(
                status_code=400, 
                detail=f"Minimum purchase is ${settings.MIN_CREDIT_PURCHASE / settings.CREDITS_PER_DOLLAR}"
            )
        
        # Create checkout session for one-time payment
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'product_data': {
                        'name': f'{credits} Credits',
                        'description': f'ResonantGenesis Platform Credits',
                    },
                    'unit_amount': int(request.amount_usd * 100),  # Stripe uses cents
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url=f'{settings.FRONTEND_URL}/billing?credits_success=true',
            cancel_url=f'{settings.FRONTEND_URL}/billing?credits_canceled=true',
            metadata={
                'user_id': user_id,
                'credits': str(credits),
                'amount_usd': str(request.amount_usd),
                'type': 'credit_purchase',
            },
        )
        
        return {
            "status": "success",
            "checkout_url": checkout_session.url,
            "session_id": checkout_session.id,
            "credits": credits,
        }
        
    except Exception as e:
        logger.error(f"Credit checkout session creation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/admin/stats")
async def get_admin_stats(
    session: AsyncSession = Depends(get_session),
):
    """Get platform-wide billing statistics (admin only).
    
    Returns global billing metrics across ALL users - for owner dashboard.
    No user filtering applied.
    
    IMPORTANT: Revenue and paying_users are ONLY counted from Stripe-confirmed
    transactions (stripe_payment_intent_id IS NOT NULL). Free credits, bonuses,
    referrals, and rollovers are tracked separately.
    """
    from sqlalchemy import select, func
    from .models import CreditBalance, CreditTransaction, Subscription
    
    # --- Total credits granted (all sources: free, bonus, purchase, referral) ---
    all_positive_result = await session.execute(
        select(func.sum(CreditTransaction.amount))
        .where(CreditTransaction.amount > 0)
    )
    total_credits_granted = all_positive_result.scalar() or 0
    
    # --- Credits from REAL Stripe purchases only ---
    stripe_purchased_result = await session.execute(
        select(func.sum(CreditTransaction.amount))
        .where(CreditTransaction.amount > 0)
        .where(CreditTransaction.stripe_payment_intent_id.isnot(None))
    )
    total_credits_purchased = stripe_purchased_result.scalar() or 0
    
    # --- Free/bonus credits (everything NOT from Stripe) ---
    total_credits_free = total_credits_granted - total_credits_purchased
    
    # --- Total credits used (all time) ---
    used_result = await session.execute(
        select(func.sum(func.abs(CreditTransaction.amount)))
        .where(CreditTransaction.amount < 0)
    )
    total_credits_used = used_result.scalar() or 0
    
    # --- Active subscriptions count ---
    active_subs_result = await session.execute(
        select(func.count(Subscription.id))
        .where(Subscription.status == "active")
    )
    active_subscriptions = active_subs_result.scalar() or 0
    
    # --- REAL revenue: only from Stripe-confirmed purchases ---
    # Credits with stripe_payment_intent_id are real purchases at $0.01/credit
    total_revenue = float(total_credits_purchased) * 0.01 if total_credits_purchased else 0.0
    
    # --- REAL paying users: only users with Stripe-confirmed transactions ---
    paying_users_result = await session.execute(
        select(func.count(func.distinct(CreditTransaction.user_id)))
        .where(CreditTransaction.amount > 0)
        .where(CreditTransaction.stripe_payment_intent_id.isnot(None))
    )
    paying_users = paying_users_result.scalar() or 0
    
    # --- Users who received ANY credits (including free) ---
    all_credit_users_result = await session.execute(
        select(func.count(func.distinct(CreditTransaction.user_id)))
        .where(CreditTransaction.amount > 0)
    )
    users_with_credits = all_credit_users_result.scalar() or 0
    
    return {
        "total_credits_purchased": total_credits_purchased,
        "total_credits_granted": total_credits_granted,
        "total_credits_free": total_credits_free,
        "total_credits_used": total_credits_used,
        "active_subscriptions": active_subscriptions,
        "total_revenue": total_revenue,
        "paying_users": paying_users,
        "users_with_credits": users_with_credits,
    }


# Health check
@router.get("/health")
async def health():
    return {"status": "ok", "service": "billing"}


# ============================================
# PRICING API - Public endpoint for frontend
# ============================================

@router.get("/pricing")
async def get_pricing_config():
    """
    Get complete pricing configuration.
    This is the SINGLE SOURCE OF TRUTH for all pricing data.
    Frontend should fetch this instead of using hardcoded values.
    """
    from .pricing_loader import load_pricing_config
    
    config = load_pricing_config()
    
    return {
        "credit_rate": config.get("credit_rate", {}),
        "plans": config.get("plans", {}),
        "credit_packs": config.get("credit_packs", []),
        "credit_costs": config.get("credit_costs", {}),
        "tier_mappings": config.get("tier_mappings", {}),
    }


@router.get("/pricing/plans")
async def get_plans():
    """Get all subscription plans."""
    from .pricing_loader import get_plans as _get_plans
    return _get_plans()


@router.get("/pricing/plans/{plan_id}")
async def get_plan(plan_id: str):
    """Get a specific plan by ID."""
    from .pricing_loader import get_plan as _get_plan
    plan = _get_plan(plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail=f"Plan '{plan_id}' not found")
    return plan


@router.get("/pricing/credit-packs")
async def get_credit_packs():
    """Get all credit pack options."""
    from .pricing_loader import get_credit_packs as _get_credit_packs
    return _get_credit_packs()


@router.get("/packs")
async def get_packs_alias():
    """Get credit packs - alias for /pricing/credit-packs for frontend compatibility."""
    from .pricing_loader import get_credit_packs as _get_credit_packs
    return _get_credit_packs()


@router.get("/pricing/credit-costs")
async def get_credit_costs():
    """Get all credit cost configurations."""
    from .pricing_loader import get_credit_costs as _get_credit_costs
    return _get_credit_costs()


# ============================================
# STRIPE CHECKOUT ENDPOINTS
# ============================================

class CheckoutCreditsRequest(BaseModel):
    pack_id: str
    success_url: Optional[str] = None
    cancel_url: Optional[str] = None


class CheckoutSubscriptionRequest(BaseModel):
    plan_id: str
    billing_cycle: str = "monthly"
    success_url: Optional[str] = None
    cancel_url: Optional[str] = None


@router.post("/checkout/credits")
async def create_credits_checkout(
    request: CheckoutCreditsRequest,
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Create Stripe checkout session for credit pack purchase."""
    import stripe
    from .pricing_loader import get_credit_packs
    
    stripe.api_key = settings.STRIPE_SECRET_KEY
    
    # Find the credit pack
    packs = get_credit_packs()
    pack = next((p for p in packs if p["id"] == request.pack_id), None)
    
    if not pack:
        raise HTTPException(status_code=404, detail=f"Credit pack '{request.pack_id}' not found")
    
    # Get or create Stripe customer
    customer_id = await _get_or_create_stripe_customer(user_id, session)
    
    try:
        # Create checkout with dynamic price_data (no pre-configured Stripe products needed)
        checkout_session = stripe.checkout.Session.create(
            customer=customer_id,
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": pack.get("name", f"{pack['credits']:,} Credits"),
                        "description": f"{pack['credits']:,} ResonantGenesis credits. ${pack.get('price_per_1k', 1.0):.2f} per 1K credits.",
                    },
                    "unit_amount": int(pack["price"] * 100),  # Stripe uses cents
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url=request.success_url or f"{settings.FRONTEND_URL}/billing?success=true&credits={pack['credits']}",
            cancel_url=request.cancel_url or f"{settings.FRONTEND_URL}/billing?canceled=true",
            metadata={
                "user_id": user_id,
                "pack_id": request.pack_id,
                "credits": pack["credits"],
                "type": "credit_purchase",
            },
        )
        
        return {
            "checkout_url": checkout_session.url,
            "session_id": checkout_session.id,
            "pack": pack,
        }
    except stripe.error.StripeError as e:
        logger.error(f"Stripe checkout error: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/checkout/subscription")
async def create_subscription_checkout(
    request: CheckoutSubscriptionRequest,
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Create Stripe checkout session for subscription."""
    import stripe
    from .pricing_loader import get_plan
    
    stripe.api_key = settings.STRIPE_SECRET_KEY
    
    # Find the plan
    plan = get_plan(request.plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail=f"Plan '{request.plan_id}' not found")
    
    # Get price based on billing cycle
    price_info = plan.get("price", {})
    if request.billing_cycle == "yearly":
        amount = price_info.get("yearly", 490)
        interval = "year"
    else:
        amount = price_info.get("monthly", 49)
        interval = "month"
    
    if amount == 0:
        raise HTTPException(status_code=400, detail="Cannot checkout free plan")
    
    # Get or create Stripe customer
    customer_id = await _get_or_create_stripe_customer(user_id, session)
    
    # Build description - only show credits (that's what gets deducted)
    credits = plan.get("credits", {})
    credits_included = credits.get("included", 75000)
    
    description = f"{credits_included:,} credits/month. Use credits for chat, agents, code execution, workflows, and more."
    
    try:
        # Create checkout with dynamic price_data (correct description from our config)
        checkout_session = stripe.checkout.Session.create(
            customer=customer_id,
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": f"ResonantGenesis {plan.get('name', 'Plus')}",
                        "description": description,
                    },
                    "unit_amount": int(amount * 100),  # Stripe uses cents
                    "recurring": {
                        "interval": interval,
                    },
                },
                "quantity": 1,
            }],
            mode="subscription",
            success_url=request.success_url or f"{settings.FRONTEND_URL}/billing?success=true&plan={request.plan_id}",
            cancel_url=request.cancel_url or f"{settings.FRONTEND_URL}/billing?canceled=true",
            metadata={
                "user_id": user_id,
                "plan_id": request.plan_id,
                "billing_cycle": request.billing_cycle,
                "type": "subscription",
            },
        )
        
        return {
            "checkout_url": checkout_session.url,
            "session_id": checkout_session.id,
            "plan": plan,
        }
    except stripe.error.StripeError as e:
        logger.error(f"Stripe checkout error: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/webhook")
async def stripe_webhook(request: Request, session: AsyncSession = Depends(get_session)):
    """Handle Stripe webhooks."""
    import stripe
    
    stripe.api_key = settings.STRIPE_SECRET_KEY
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    
    # Handle the event
    if event["type"] == "checkout.session.completed":
        session_data = event["data"]["object"]
        metadata = session_data.get("metadata", {})
        
        if metadata.get("type") == "credit_purchase":
            # Fulfill credit purchase
            user_id = metadata.get("user_id")
            credits = int(metadata.get("credits", 0))
            pack_id = metadata.get("pack_id")
            
            if user_id and credits > 0:
                await credit_manager.add_credits(
                    user_id=user_id,
                    amount=credits,
                    tx_type="purchase",
                    reference_type="stripe_checkout",
                    reference_id=session_data["id"],
                    description=f"Credit pack purchase: {pack_id}",
                    db_session=session,
                )
                logger.info(f"Fulfilled {credits} credits for user {user_id}")
        
        elif metadata.get("type") == "subscription":
            # Handle subscription creation
            user_id = metadata.get("user_id")
            plan_id = metadata.get("plan_id")
            billing_cycle = metadata.get("billing_cycle", "monthly")
            
            if user_id and plan_id:
                # Update subscription in database
                await subscription_manager.create_subscription(
                    user_id=user_id,
                    plan=plan_id,
                    billing_cycle=billing_cycle,
                    stripe_subscription_id=session_data.get("subscription"),
                    stripe_customer_id=session_data.get("customer"),
                    db_session=session,
                )
                logger.info(f"Created subscription {plan_id} for user {user_id}")
    
    elif event["type"] == "customer.subscription.updated":
        # Handle subscription updates
        subscription = event["data"]["object"]
        # Update local subscription status
        pass
    
    elif event["type"] == "customer.subscription.deleted":
        # Handle subscription cancellation
        subscription = event["data"]["object"]
        # Mark subscription as canceled
        pass
    
    elif event["type"] == "invoice.paid":
        # Handle successful invoice payment
        invoice = event["data"]["object"]
        # Could add bonus credits for loyalty
        pass
    
    return {"status": "ok"}


@router.get("/portal")
async def get_customer_portal(
    user_id: str = Depends(get_user_id),
    session: AsyncSession = Depends(get_session),
):
    """Get Stripe Customer Portal URL for managing subscriptions."""
    import stripe
    
    stripe.api_key = settings.STRIPE_SECRET_KEY
    
    customer_id = await _get_or_create_stripe_customer(user_id, session)
    
    try:
        portal_session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{settings.FRONTEND_URL}/billing",
        )
        return {"portal_url": portal_session.url}
    except stripe.error.StripeError as e:
        logger.error(f"Stripe portal error: {e}")
        raise HTTPException(status_code=400, detail=str(e))


async def _get_or_create_stripe_customer(user_id: str, session: AsyncSession) -> str:
    """Get existing Stripe customer ID or create new one."""
    import stripe
    from .models import Subscription
    
    stripe.api_key = settings.STRIPE_SECRET_KEY
    
    # Check if user already has a Stripe customer ID
    result = await session.execute(
        select(Subscription).where(Subscription.user_id == user_id)
    )
    subscription = result.scalar_one_or_none()
    
    if subscription and subscription.stripe_customer_id:
        return subscription.stripe_customer_id
    
    # Create new Stripe customer
    try:
        customer = stripe.Customer.create(
            metadata={"user_id": user_id}
        )
        return customer.id
    except stripe.error.StripeError as e:
        logger.error(f"Failed to create Stripe customer: {e}")
        raise HTTPException(status_code=400, detail="Failed to create payment profile")
