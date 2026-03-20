"""
Referral Credit System

Grants credits when users refer friends:
- Referrer gets 5,000 bonus credits
- Referred user gets 2,000 welcome bonus
"""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from uuid import UUID
import logging

from .models import CreditBalance, CreditTransaction
from .economic_state import UserEconomicState

logger = logging.getLogger(__name__)

# Referral credit amounts
REFERRER_BONUS = 5_000  # Credits for the person who referred
REFERRED_BONUS = 2_000  # Welcome bonus for new user


async def grant_referral_credits(
    referrer_user_id: UUID,
    referred_user_id: UUID,
    referral_code: str,
    db_session: AsyncSession,
) -> dict:
    """
    Grant referral credits to both referrer and referred user.
    
    Args:
        referrer_user_id: User who made the referral
        referred_user_id: New user who was referred
        referral_code: Referral code used
        db_session: Database session
    
    Returns:
        dict with granted amounts
    """
    try:
        # Grant credits to referrer
        result = await db_session.execute(
            select(UserEconomicState)
            .where(UserEconomicState.user_id == referrer_user_id)
            .with_for_update()
        )
        referrer_state = result.scalar_one_or_none()
        
        if referrer_state:
            referrer_state.credit_balance += REFERRER_BONUS
            
            # Create transaction record
            referrer_tx = CreditTransaction(
                user_id=str(referrer_user_id),
                tx_type="referral_reward",
                amount=REFERRER_BONUS,
                balance_after=referrer_state.credit_balance,
                description=f"Referral reward for code: {referral_code}",
                reference_type="referral",
                reference_id=str(referred_user_id),
            )
            db_session.add(referrer_tx)
            logger.info(f"Granted {REFERRER_BONUS} credits to referrer {referrer_user_id}")
        
        # Grant welcome bonus to referred user
        result = await db_session.execute(
            select(UserEconomicState)
            .where(UserEconomicState.user_id == referred_user_id)
            .with_for_update()
        )
        referred_state = result.scalar_one_or_none()
        
        if referred_state:
            referred_state.credit_balance += REFERRED_BONUS
            
            # Create transaction record
            referred_tx = CreditTransaction(
                user_id=str(referred_user_id),
                tx_type="referral_bonus",
                amount=REFERRED_BONUS,
                balance_after=referred_state.credit_balance,
                description=f"Welcome bonus from referral code: {referral_code}",
                reference_type="referral",
                reference_id=str(referrer_user_id),
            )
            db_session.add(referred_tx)
            logger.info(f"Granted {REFERRED_BONUS} credits to referred user {referred_user_id}")
        
        await db_session.commit()
        
        return {
            "referrer_bonus": REFERRER_BONUS if referrer_state else 0,
            "referred_bonus": REFERRED_BONUS if referred_state else 0,
            "success": True,
        }
        
    except Exception as e:
        logger.error(f"Failed to grant referral credits: {e}")
        await db_session.rollback()
        return {
            "referrer_bonus": 0,
            "referred_bonus": 0,
            "success": False,
            "error": str(e),
        }
