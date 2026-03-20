"""
Scheduled Jobs for Credit management cron jobs.

Production setup with APScheduler for automated daily execution.
- Credit expiration: Daily at midnight (00:00 UTC)
- Credit rollover: Daily at midnight (00:00 UTC)
- Stripe subscription sync: Daily at 1:00 AM UTC
"""

import asyncio
from datetime import datetime, timedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .database import get_db, async_session_maker
from .models import CreditTransaction, CreditBalance, Subscription
from .credits import CreditManager
from .credit_rollover import CreditRolloverService
import logging

logger = logging.getLogger(__name__)

# Global scheduler instance
scheduler: AsyncIOScheduler | None = None

credit_manager = CreditManager()
rollover_service = CreditRolloverService()


async def run_credit_expiration():
    """
    Run daily to expire old credits.
    
    Schedule: 0 0 * * * (midnight daily)
    """
    logger.info("Starting credit expiration job")
    
    try:
        async with async_session_maker() as db:
            expired_count = await credit_manager.expire_old_credits(db)
            logger.info(f"Credit expiration complete: {expired_count} balances processed")
            return {"success": True, "expired_count": expired_count}
    except Exception as e:
        logger.error(f"Credit expiration failed: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def run_period_rollover():
    """
    Run monthly credit rollover for all users.
    Runs daily at midnight, checks if it's the 1st of the month.
    """
    # Only run on the 1st of the month
    if datetime.utcnow().day != 1:
        logger.debug("Skipping rollover - not the 1st of the month")
        return
    
    logger.info("Starting monthly credit rollover")
    
    async with async_session_maker() as db:
        try:
            # Get all active users
            result = await db.execute(
                select(UserEconomicState).where(
                    UserEconomicState.subscription_tier != SubscriptionTier.FREE
                )
            )
            states = result.scalars().all()
            
            rollover_count = 0
            for state in states:
                # Get tier config
                tier_config = TIER_DEFAULTS.get(state.subscription_tier, {})
                rollover_max = tier_config.get("credit_rollover_max", 0)
                monthly_credits = tier_config.get("credit_balance", 0)
                
                if rollover_max > 0 and monthly_credits > 0:
                    # Calculate rollover
                    current_balance = state.credit_balance
                    rollover_amount = min(current_balance, rollover_max)
                    
                    # Reset to monthly credits + rollover
                    new_balance = monthly_credits + rollover_amount
                    
                    # Log transaction
                    credit_tx = CreditTransaction(
                        user_id=state.user_id,
                        org_id=state.org_id,
                        amount=monthly_credits,
                        transaction_type="monthly_grant",
                        description=f"Monthly credit grant + {rollover_amount} rollover",
                        balance_after=new_balance,
                    )
                    db.add(credit_tx)
                    
                    state.credit_balance = new_balance
                    rollover_count += 1
                else:
                    # No rollover, just reset to monthly credits
                    if monthly_credits > 0:
                        state.credit_balance = monthly_credits
                        
                        credit_tx = CreditTransaction(
                            user_id=state.user_id,
                            org_id=state.org_id,
                            amount=monthly_credits,
                            transaction_type="monthly_grant",
                            description="Monthly credit grant",
                            balance_after=monthly_credits,
                        )
                        db.add(credit_tx)
                        rollover_count += 1
            
            await db.commit()
            logger.info(f"Credit rollover complete: {rollover_count} users processed")
            
        except Exception as e:
            logger.error(f"Error during credit rollover: {e}", exc_info=True)
            await db.rollback()


async def handle_stripe_payment_failure(user_id: str, org_id: str):
    """
    Handle Stripe payment failure - downgrade user to Developer tier.
    Called by Stripe webhook when payment fails.
    """
    logger.warning(f"Handling payment failure for user {user_id}")
    
    async with async_session_maker() as db:
        try:
            # Get user's economic state
            result = await db.execute(
                select(UserEconomicState).where(
                    UserEconomicState.user_id == user_id
                )
            )
            state = result.scalar_one_or_none()
            
            if not state:
                logger.error(f"No economic state found for user {user_id}")
                return
            
            # Downgrade to Developer tier
            old_tier = state.subscription_tier
            state.subscription_tier = SubscriptionTier.DEVELOPER
            state.subscription_status = "payment_failed"
            
            # Reset credits to Developer tier (10,000)
            developer_credits = TIER_DEFAULTS[SubscriptionTier.DEVELOPER]["credit_balance"]
            state.credit_balance = developer_credits
            
            # Log transaction
            credit_tx = CreditTransaction(
                user_id=state.user_id,
                org_id=state.org_id,
                amount=developer_credits - state.credit_balance,
                transaction_type="payment_failure_downgrade",
                description=f"Downgraded from {old_tier} due to payment failure",
                balance_after=developer_credits,
            )
            db.add(credit_tx)
            
            await db.commit()
            logger.info(f"Downgraded user {user_id} from {old_tier} to Developer due to payment failure")
            
        except Exception as e:
            logger.error(f"Error handling payment failure: {e}", exc_info=True)
            await db.rollback()


async def run_all_daily_jobs():
    """Run all daily credit management jobs."""
    logger.info("Starting all daily credit jobs")
    
    # Run jobs in sequence
    expiration_result = await run_credit_expiration()
    rollover_result = await run_period_rollover()
    
    return {
        "expiration": expiration_result,
        "rollover": rollover_result,
        "timestamp": datetime.utcnow().isoformat(),
    }


# For manual execution
if __name__ == "__main__":
    result = asyncio.run(run_all_daily_jobs())
    print(f"Daily jobs complete: {result}")


def start_scheduler():
    """
    Start the APScheduler for automated cron jobs.
    Called on application startup.
    """
    global scheduler
    
    if scheduler is not None:
        logger.warning("Scheduler already running")
        return
    
    scheduler = AsyncIOScheduler()
    
    # Credit expiration: Daily at midnight UTC
    scheduler.add_job(
        run_credit_expiration,
        trigger=CronTrigger(hour=0, minute=0),
        id="credit_expiration",
        name="Daily credit expiration check",
        replace_existing=True,
    )
    
    # Credit rollover: Daily at midnight UTC (checks if 1st of month)
    scheduler.add_job(
        run_period_rollover,
        trigger=CronTrigger(hour=0, minute=0),
        id="credit_rollover",
        name="Monthly credit rollover",
        replace_existing=True,
    )
    
    scheduler.start()
    logger.info("✅ Cron scheduler started - jobs will run daily at midnight UTC")


def stop_scheduler():
    """
    Stop the APScheduler.
    Called on application shutdown.
    """
    global scheduler
    
    if scheduler is not None:
        scheduler.shutdown()
        scheduler = None
        logger.info("Cron scheduler stopped")
