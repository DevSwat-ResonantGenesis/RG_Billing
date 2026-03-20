"""
Billing Dashboard API - Phase 2.4 GTM

Comprehensive billing dashboard endpoints for self-service billing management.
"""

import logging
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from .db import get_session
from .models import CreditBalance, CreditTransaction, Subscription
from .economic_state import UserEconomicState, TIER_DEFAULTS
from .cost_estimator import cost_estimator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


# ============================================
# RESPONSE MODELS
# ============================================

class UsageByService(BaseModel):
    """Usage breakdown by service."""
    service: str
    credits: int
    percentage: float
    transaction_count: int


class UsageDataPoint(BaseModel):
    """Single data point for usage chart."""
    date: str
    credits_used: int
    transaction_count: int


class BillingDashboard(BaseModel):
    """Complete billing dashboard data."""
    current_balance: int
    tier: str
    tier_credits: int
    usage_this_period: int
    usage_percent: float
    days_remaining: int
    next_billing_date: Optional[str]
    estimated_monthly_cost: int
    alerts: List[Dict[str, Any]]
    recent_transactions: List[Dict[str, Any]]
    usage_by_service: List[UsageByService]


class CostEstimateRequest(BaseModel):
    """Request for cost estimation."""
    operation_type: str  # chat, agent, workflow, code_execution
    # Chat params
    message_length: Optional[int] = None
    model: Optional[str] = "gpt-4o"
    provider: Optional[str] = "openai"
    # Agent params
    agent_type: Optional[str] = "general"
    estimated_steps: Optional[int] = 5
    # Workflow params
    node_count: Optional[int] = None
    has_parallel: Optional[bool] = False
    # Code execution params
    estimated_seconds: Optional[int] = 10


# ============================================
# DASHBOARD ENDPOINTS
# ============================================

@router.get("/{user_id}", response_model=BillingDashboard)
async def get_billing_dashboard(
    user_id: str,
    db: AsyncSession = Depends(get_session),
) -> BillingDashboard:
    """
    Get complete billing dashboard data for a user.
    
    Returns:
    - Current balance and tier info
    - Usage statistics for current period
    - Recent transactions
    - Usage breakdown by service
    - Active alerts
    """
    # Get economic state - auto-create if not exists
    result = await db.execute(
        select(UserEconomicState).where(UserEconomicState.user_id == user_id)
    )
    state = result.scalar_one_or_none()
    
    if not state:
        # Auto-create economic state for new users with free tier
        state = UserEconomicState(
            user_id=user_id,
            org_id=user_id,  # Default org_id to user_id
            subscription_tier="developer",
            credit_balance=1000,  # Default free tier credits (1k/month)
        )
        db.add(state)
        await db.commit()
        await db.refresh(state)
    
    # Get tier defaults
    tier_defaults = TIER_DEFAULTS.get(state.subscription_tier, {})
    tier_credits = tier_defaults.get("credit_balance", 1000)
    
    # Get actual credit balance from credit_balances table (where deductions are recorded)
    balance_result = await db.execute(
        select(CreditBalance).where(CreditBalance.user_id == user_id)
    )
    credit_balance = balance_result.scalar_one_or_none()
    current_balance = credit_balance.balance if credit_balance else tier_credits
    
    # Get billing period from credit_balance or use 30-day cycle from creation
    now = datetime.utcnow()
    if credit_balance and hasattr(credit_balance, 'period_start') and credit_balance.period_start:
        period_start = credit_balance.period_start.replace(tzinfo=None) if credit_balance.period_start.tzinfo else credit_balance.period_start
    elif credit_balance and credit_balance.created_at:
        # Use creation date as period start
        period_start = credit_balance.created_at.replace(tzinfo=None) if credit_balance.created_at.tzinfo else credit_balance.created_at
    else:
        # Fallback to first of current month
        period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    usage_result = await db.execute(
        select(func.sum(func.abs(CreditTransaction.amount)))
        .where(
            CreditTransaction.user_id == user_id,
            CreditTransaction.tx_type == "usage",
            CreditTransaction.created_at >= period_start,
        )
    )
    usage_this_period = usage_result.scalar() or 0
    
    # Calculate usage percent
    usage_percent = 0.0
    if tier_credits > 0:
        usage_percent = (usage_this_period / tier_credits) * 100
    
    # Days remaining in period
    next_billing = period_start + timedelta(days=30)
    days_remaining = max(0, (next_billing - datetime.utcnow()).days)
    
    # Get recent transactions
    tx_result = await db.execute(
        select(CreditTransaction)
        .where(CreditTransaction.user_id == user_id)
        .order_by(CreditTransaction.created_at.desc())
        .limit(10)
    )
    transactions = tx_result.scalars().all()
    
    recent_transactions = [
        {
            "id": str(tx.id),
            "type": tx.tx_type,
            "amount": tx.amount,
            "balance_after": tx.balance_after,
            "reference_type": tx.reference_type,
            "description": tx.description,
            "created_at": tx.created_at.isoformat() if tx.created_at else None,
        }
        for tx in transactions
    ]
    
    # Get usage by service
    service_result = await db.execute(
        select(
            CreditTransaction.reference_type,
            func.sum(func.abs(CreditTransaction.amount)).label("total"),
            func.count().label("count"),
        )
        .where(
            CreditTransaction.user_id == user_id,
            CreditTransaction.tx_type == "usage",
            CreditTransaction.created_at >= period_start,
        )
        .group_by(CreditTransaction.reference_type)
    )
    service_usage = service_result.all()
    
    usage_by_service = []
    for row in service_usage:
        service_name = row.reference_type or "other"
        credits = int(row.total or 0)
        percentage = (credits / usage_this_period * 100) if usage_this_period > 0 else 0
        usage_by_service.append(UsageByService(
            service=service_name,
            credits=credits,
            percentage=round(percentage, 1),
            transaction_count=row.count,
        ))
    
    # Sort by credits descending
    usage_by_service.sort(key=lambda x: x.credits, reverse=True)
    
    # Estimate monthly cost based on current usage rate
    if days_remaining > 0:
        days_elapsed = 30 - days_remaining
        if days_elapsed > 0:
            daily_rate = usage_this_period / days_elapsed
            estimated_monthly = int(daily_rate * 30)
        else:
            estimated_monthly = 0
    else:
        estimated_monthly = usage_this_period
    
    # Get active alerts
    alerts = []
    if usage_percent >= 100:
        alerts.append({
            "level": "exhausted_100",
            "message": "Credits exhausted - please upgrade or purchase more",
            "priority": "critical",
        })
    elif usage_percent >= 90:
        alerts.append({
            "level": "critical_90",
            "message": "Only 10% of credits remaining",
            "priority": "high",
        })
    elif usage_percent >= 80:
        alerts.append({
            "level": "warning_80",
            "message": "You've used 80% of your credits",
            "priority": "medium",
        })
    
    return BillingDashboard(
        current_balance=current_balance,
        tier=state.subscription_tier.value if hasattr(state.subscription_tier, 'value') else str(state.subscription_tier),
        tier_credits=tier_credits,
        usage_this_period=int(usage_this_period),
        usage_percent=round(usage_percent, 1),
        days_remaining=days_remaining,
        next_billing_date=next_billing.isoformat() if next_billing else None,
        estimated_monthly_cost=estimated_monthly,
        alerts=alerts,
        recent_transactions=recent_transactions,
        usage_by_service=usage_by_service,
    )


@router.get("/{user_id}/usage-chart")
async def get_usage_chart(
    user_id: str,
    period: str = Query("30d", regex="^(7d|30d|90d)$"),
    db: AsyncSession = Depends(get_session),
) -> List[UsageDataPoint]:
    """
    Get usage data for charts.
    
    Args:
        user_id: User ID
        period: Time period (7d, 30d, 90d)
        
    Returns:
        List of daily usage data points
    """
    # Parse period
    days = {"7d": 7, "30d": 30, "90d": 90}[period]
    start_date = datetime.utcnow() - timedelta(days=days)
    
    # Get daily usage
    result = await db.execute(
        select(
            func.date(CreditTransaction.created_at).label("date"),
            func.sum(func.abs(CreditTransaction.amount)).label("credits"),
            func.count().label("count"),
        )
        .where(
            CreditTransaction.user_id == user_id,
            CreditTransaction.tx_type == "usage",
            CreditTransaction.created_at >= start_date,
        )
        .group_by(func.date(CreditTransaction.created_at))
        .order_by(func.date(CreditTransaction.created_at))
    )
    rows = result.all()
    
    # Create data points
    data_points = []
    for row in rows:
        data_points.append(UsageDataPoint(
            date=str(row.date),
            credits_used=int(row.credits or 0),
            transaction_count=row.count,
        ))
    
    return data_points


@router.get("/{user_id}/breakdown")
async def get_usage_breakdown(
    user_id: str,
    db: AsyncSession = Depends(get_session),
) -> Dict[str, int]:
    """
    Get usage breakdown by service for current period.
    
    Returns:
        Dict mapping service names to credit usage
    """
    # Get current period start
    period_start = datetime.utcnow().replace(day=1)
    
    result = await db.execute(
        select(
            CreditTransaction.reference_type,
            func.sum(func.abs(CreditTransaction.amount)).label("total"),
        )
        .where(
            CreditTransaction.user_id == user_id,
            CreditTransaction.tx_type == "usage",
            CreditTransaction.created_at >= period_start,
        )
        .group_by(CreditTransaction.reference_type)
    )
    rows = result.all()
    
    breakdown = {}
    for row in rows:
        service = row.reference_type or "other"
        breakdown[service] = int(row.total or 0)
    
    return breakdown


@router.get("/{user_id}/transactions")
async def get_transactions(
    user_id: str,
    tx_type: Optional[str] = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_session),
) -> Dict[str, Any]:
    """
    Get paginated transaction history.
    
    Args:
        user_id: User ID
        tx_type: Optional filter by transaction type
        limit: Number of transactions to return
        offset: Offset for pagination
        
    Returns:
        Paginated transaction list with total count
    """
    # Build query
    query = select(CreditTransaction).where(CreditTransaction.user_id == user_id)
    count_query = select(func.count()).select_from(CreditTransaction).where(
        CreditTransaction.user_id == user_id
    )
    
    if tx_type:
        query = query.where(CreditTransaction.tx_type == tx_type)
        count_query = count_query.where(CreditTransaction.tx_type == tx_type)
    
    # Get total count
    total_result = await db.execute(count_query)
    total = total_result.scalar()
    
    # Get transactions
    query = query.order_by(CreditTransaction.created_at.desc())
    query = query.limit(limit).offset(offset)
    
    result = await db.execute(query)
    transactions = result.scalars().all()
    
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "transactions": [
            {
                "id": str(tx.id),
                "type": tx.tx_type,
                "amount": tx.amount,
                "balance_after": tx.balance_after,
                "reference_type": tx.reference_type,
                "reference_id": tx.reference_id,
                "description": tx.description,
                "created_at": tx.created_at.isoformat() if tx.created_at else None,
            }
            for tx in transactions
        ],
    }


@router.post("/estimate")
async def estimate_cost(request: CostEstimateRequest) -> Dict[str, Any]:
    """
    Estimate cost before execution.
    
    Supports:
    - chat: Estimate chat message cost
    - agent: Estimate agent run cost
    - workflow: Estimate workflow execution cost
    - code_execution: Estimate code execution cost
    """
    if request.operation_type == "chat":
        if not request.message_length:
            raise HTTPException(400, "message_length required for chat estimation")
        
        estimate = cost_estimator.estimate_chat(
            message_length=request.message_length,
            model=request.model or "gpt-4o",
            provider=request.provider or "openai",
        )
        
    elif request.operation_type == "agent":
        estimate = cost_estimator.estimate_agent_run(
            agent_type=request.agent_type or "general",
            estimated_steps=request.estimated_steps or 5,
        )
        
    elif request.operation_type == "workflow":
        if not request.node_count:
            raise HTTPException(400, "node_count required for workflow estimation")
        
        estimate = cost_estimator.estimate_workflow(
            node_count=request.node_count,
            has_parallel=request.has_parallel or False,
        )
        
    elif request.operation_type == "code_execution":
        estimate = cost_estimator.estimate_code_execution(
            estimated_seconds=request.estimated_seconds or 10,
        )
        
    else:
        raise HTTPException(400, f"Unknown operation type: {request.operation_type}")
    
    return estimate.to_dict()


@router.get("/{user_id}/summary")
async def get_usage_summary(
    user_id: str,
    db: AsyncSession = Depends(get_session),
) -> Dict[str, Any]:
    """
    Get usage summary with trends and recommendations.
    """
    # Get economic state
    result = await db.execute(
        select(UserEconomicState).where(UserEconomicState.user_id == user_id)
    )
    state = result.scalar_one_or_none()
    
    if not state:
        raise HTTPException(404, "User not found")
    
    # Get tier info
    tier_defaults = TIER_DEFAULTS.get(state.subscription_tier, {})
    tier_credits = tier_defaults.get("credit_balance", 1000)
    
    # Get usage for last 30 days
    thirty_days_ago = datetime.utcnow() - timedelta(days=30)
    
    usage_result = await db.execute(
        select(func.sum(func.abs(CreditTransaction.amount)))
        .where(
            CreditTransaction.user_id == user_id,
            CreditTransaction.tx_type == "usage",
            CreditTransaction.created_at >= thirty_days_ago,
        )
    )
    usage_30d = usage_result.scalar() or 0
    
    # Get usage for previous 30 days (for trend)
    sixty_days_ago = datetime.utcnow() - timedelta(days=60)
    
    prev_usage_result = await db.execute(
        select(func.sum(func.abs(CreditTransaction.amount)))
        .where(
            CreditTransaction.user_id == user_id,
            CreditTransaction.tx_type == "usage",
            CreditTransaction.created_at >= sixty_days_ago,
            CreditTransaction.created_at < thirty_days_ago,
        )
    )
    usage_prev_30d = prev_usage_result.scalar() or 0
    
    # Calculate trend
    if usage_prev_30d > 0:
        trend_percent = ((usage_30d - usage_prev_30d) / usage_prev_30d) * 100
        if trend_percent > 10:
            trend = "increasing"
        elif trend_percent < -10:
            trend = "decreasing"
        else:
            trend = "stable"
    else:
        trend = "new_user"
        trend_percent = 0
    
    # Project monthly usage
    daily_avg = usage_30d / 30
    projected_monthly = int(daily_avg * 30)
    
    # Days until exhausted
    if daily_avg > 0 and state.credit_balance > 0:
        days_until_exhausted = int(state.credit_balance / daily_avg)
    else:
        days_until_exhausted = -1  # Unlimited or no usage
    
    # Generate recommendations
    recommendations = []
    
    if projected_monthly > tier_credits * 0.9:
        recommendations.append({
            "type": "upgrade",
            "message": "Consider upgrading to avoid running out of credits",
            "priority": "high",
        })
    
    if trend == "increasing" and trend_percent > 50:
        recommendations.append({
            "type": "usage_spike",
            "message": f"Usage increased {int(trend_percent)}% from last month",
            "priority": "medium",
        })
    
    if days_until_exhausted > 0 and days_until_exhausted < 7:
        recommendations.append({
            "type": "low_balance",
            "message": f"Credits may run out in {days_until_exhausted} days",
            "priority": "high",
        })
    
    return {
        "summary": {
            "total_credits_used_30d": int(usage_30d),
            "daily_average": int(daily_avg),
            "projected_monthly": projected_monthly,
            "current_balance": state.credit_balance,
            "tier_credits": tier_credits,
        },
        "trends": {
            "direction": trend,
            "percent_change": round(trend_percent, 1),
            "days_until_exhausted": days_until_exhausted,
        },
        "recommendations": recommendations,
    }
