"""
Credit Rollover Enforcement - Phase 3.3 GTM

Manage credit rollover between billing periods.
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# Rollover limits by tier (from pricing.yaml)
ROLLOVER_LIMITS = {
    "developer": 0,        # No rollover for developer tier
    "free": 0,             # Legacy alias -> developer
    "plus": 37500,         # Up to 37.5K credits
    "pro": 37500,          # Legacy alias -> plus
    "enterprise": -1,      # Unlimited rollover
}

# Monthly credit allocations by tier
TIER_CREDITS = {
    "developer": 1000,
    "free": 1000,          # Legacy alias -> developer
    "plus": 75000,
    "pro": 75000,          # Legacy alias -> plus
    "enterprise": -1,      # Unlimited
}


@dataclass
class RolloverResult:
    """Result of rollover processing."""
    user_id: str
    tier: str
    previous_balance: int
    rollover_amount: int
    new_credits: int
    new_balance: int
    rollover_capped: bool
    cap_amount: int
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "tier": self.tier,
            "previous_balance": self.previous_balance,
            "rollover_amount": self.rollover_amount,
            "new_credits": self.new_credits,
            "new_balance": self.new_balance,
            "rollover_capped": self.rollover_capped,
            "cap_amount": self.cap_amount,
        }


class CreditRolloverService:
    """
    Manage credit rollover between billing periods.
    
    Features:
    - Tier-based rollover limits
    - Automatic period processing
    - Transaction logging
    - Rollover cap enforcement
    """
    
    def get_rollover_limit(self, tier: str) -> int:
        """
        Get rollover limit for a tier.
        
        Args:
            tier: Subscription tier
            
        Returns:
            Rollover limit (-1 for unlimited, 0 for none)
        """
        tier_lower = tier.lower() if tier else "developer"
        return ROLLOVER_LIMITS.get(tier_lower, 0)
    
    def get_tier_credits(self, tier: str) -> int:
        """
        Get monthly credit allocation for a tier.
        
        Args:
            tier: Subscription tier
            
        Returns:
            Monthly credits (-1 for unlimited)
        """
        tier_lower = tier.lower() if tier else "developer"
        return TIER_CREDITS.get(tier_lower, 1000)
    
    def calculate_rollover(
        self,
        current_balance: int,
        tier: str,
    ) -> tuple:
        """
        Calculate rollover amount for a tier.
        
        Args:
            current_balance: Current credit balance
            tier: Subscription tier
            
        Returns:
            Tuple of (rollover_amount, was_capped, cap_amount)
        """
        rollover_limit = self.get_rollover_limit(tier)
        
        # No rollover for free tier
        if rollover_limit == 0:
            return 0, current_balance > 0, 0
        
        # Unlimited rollover
        if rollover_limit == -1:
            return current_balance, False, -1
        
        # Capped rollover
        if current_balance <= rollover_limit:
            return current_balance, False, rollover_limit
        else:
            return rollover_limit, True, rollover_limit
    
    async def process_period_end(
        self,
        user_id: str,
        db: AsyncSession,
    ) -> RolloverResult:
        """
        Process end of billing period for a user.
        
        Handles credit rollover and grants new period credits.
        Uses SELECT FOR UPDATE to maintain single-writer invariant.
        
        Args:
            user_id: User ID
            db: Database session
            
        Returns:
            RolloverResult with details
        """
        from .economic_state import UserEconomicState
        from .models import CreditTransaction, CreditBalance
        
        # Get user's economic state WITH ROW-LEVEL LOCK
        # This maintains the single-writer economic invariant
        result = await db.execute(
            select(UserEconomicState)
            .where(UserEconomicState.user_id == user_id)
            .with_for_update()  # Row-level lock for single-writer invariant
        )
        state = result.scalar_one_or_none()
        
        if not state:
            raise ValueError(f"User {user_id} not found")
        
        tier = state.subscription_tier.value if hasattr(state.subscription_tier, 'value') else str(state.subscription_tier)
        previous_balance = state.credit_balance
        
        # Calculate rollover
        rollover_amount, was_capped, cap_amount = self.calculate_rollover(
            previous_balance, tier
        )
        
        # Get new period credits
        new_credits = self.get_tier_credits(tier)
        if new_credits == -1:
            new_credits = 0  # Unlimited tier doesn't need grants
        
        # Calculate new balance
        new_balance = rollover_amount + new_credits
        
        # Update balance
        state.credit_balance = new_balance
        state.current_period_start = datetime.utcnow()
        
        # Also update CreditBalance if it exists (with lock for single-writer invariant)
        balance_result = await db.execute(
            select(CreditBalance)
            .where(CreditBalance.user_id == user_id)
            .with_for_update()  # Row-level lock
        )
        credit_balance = balance_result.scalar_one_or_none()
        if credit_balance:
            credit_balance.balance = new_balance
        
        # Create rollover transaction if there was rollover
        if rollover_amount > 0:
            rollover_tx = CreditTransaction(
                user_id=user_id,
                tx_type="rollover",
                amount=rollover_amount,
                balance_after=rollover_amount,
                description=f"Rolled over {rollover_amount} credits from previous period",
                reference_type="period_rollover",
            )
            db.add(rollover_tx)
        
        # Create expired credits transaction if credits were lost
        expired_credits = previous_balance - rollover_amount
        if expired_credits > 0:
            expired_tx = CreditTransaction(
                user_id=user_id,
                tx_type="expiration",
                amount=-expired_credits,
                balance_after=rollover_amount,
                description=f"Expired {expired_credits} credits (rollover cap: {cap_amount})",
                reference_type="period_expiration",
            )
            db.add(expired_tx)
        
        # Create period grant transaction
        if new_credits > 0:
            grant_tx = CreditTransaction(
                user_id=user_id,
                tx_type="period_grant",
                amount=new_credits,
                balance_after=new_balance,
                description=f"Monthly credit grant for {tier} tier",
                reference_type="period_grant",
            )
            db.add(grant_tx)
        
        await db.commit()
        
        logger.info(
            f"Processed period end for user {user_id[:8]}...: "
            f"rollover={rollover_amount}, new_credits={new_credits}, "
            f"new_balance={new_balance}"
        )
        
        return RolloverResult(
            user_id=user_id,
            tier=tier,
            previous_balance=previous_balance,
            rollover_amount=rollover_amount,
            new_credits=new_credits,
            new_balance=new_balance,
            rollover_capped=was_capped,
            cap_amount=cap_amount,
        )
    
    async def process_all_period_ends(
        self,
        db: AsyncSession,
    ) -> List[RolloverResult]:
        """
        Process period end for all users due for renewal.
        
        Should be run daily by a cron job.
        
        Args:
            db: Database session
            
        Returns:
            List of RolloverResults
        """
        from .economic_state import UserEconomicState
        
        # Find users whose period has ended
        # Period is 30 days from current_period_start
        cutoff = datetime.utcnow() - timedelta(days=30)
        
        result = await db.execute(
            select(UserEconomicState)
            .where(UserEconomicState.current_period_start <= cutoff)
        )
        users = result.scalars().all()
        
        results = []
        for user in users:
            try:
                rollover_result = await self.process_period_end(user.user_id, db)
                results.append(rollover_result)
            except Exception as e:
                logger.error(f"Failed to process rollover for user {user.user_id}: {e}")
        
        logger.info(f"Processed {len(results)} period rollovers")
        return results
    
    async def preview_rollover(
        self,
        user_id: str,
        db: AsyncSession,
    ) -> Dict[str, Any]:
        """
        Preview what would happen at period end.
        
        Args:
            user_id: User ID
            db: Database session
            
        Returns:
            Preview of rollover calculation
        """
        from .economic_state import UserEconomicState
        
        result = await db.execute(
            select(UserEconomicState).where(UserEconomicState.user_id == user_id)
        )
        state = result.scalar_one_or_none()
        
        if not state:
            raise ValueError(f"User {user_id} not found")
        
        tier = state.subscription_tier.value if hasattr(state.subscription_tier, 'value') else str(state.subscription_tier)
        current_balance = state.credit_balance
        
        rollover_amount, was_capped, cap_amount = self.calculate_rollover(
            current_balance, tier
        )
        
        new_credits = self.get_tier_credits(tier)
        if new_credits == -1:
            new_credits = 0
        
        new_balance = rollover_amount + new_credits
        credits_lost = current_balance - rollover_amount
        
        # Calculate days until period end
        period_start = state.current_period_start or datetime.utcnow()
        period_end = period_start + timedelta(days=30)
        days_remaining = max(0, (period_end - datetime.utcnow()).days)
        
        return {
            "user_id": user_id,
            "tier": tier,
            "current_balance": current_balance,
            "rollover_limit": cap_amount,
            "rollover_amount": rollover_amount,
            "credits_to_expire": credits_lost,
            "new_period_credits": new_credits,
            "projected_new_balance": new_balance,
            "period_end_date": period_end.isoformat(),
            "days_remaining": days_remaining,
        }


# Global instance
credit_rollover_service = CreditRolloverService()


# Convenience functions
async def process_user_rollover(user_id: str, db: AsyncSession) -> RolloverResult:
    """Process rollover for a single user."""
    return await credit_rollover_service.process_period_end(user_id, db)


async def preview_user_rollover(user_id: str, db: AsyncSession) -> Dict[str, Any]:
    """Preview rollover for a user."""
    return await credit_rollover_service.preview_rollover(user_id, db)
