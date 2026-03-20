"""
Compliance Features - Phase 4.3 GTM

GDPR and data privacy compliance for billing data.
"""

import logging
import hashlib
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
from enum import Enum

from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class DataRequestType(str, Enum):
    """Types of data requests."""
    EXPORT = "export"
    DELETE = "delete"
    RECTIFY = "rectify"
    RESTRICT = "restrict"


class DataRequestStatus(str, Enum):
    """Status of data requests."""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class DataExport:
    """Exported user data."""
    user_id: str
    export_date: str
    data_categories: List[str]
    data: Dict[str, Any]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "export_date": self.export_date,
            "data_categories": self.data_categories,
            "data": self.data,
        }


class ComplianceService:
    """
    GDPR and data privacy compliance service.
    
    Features:
    - Data export (Right to Access)
    - Data deletion (Right to Erasure)
    - Data rectification
    - Processing restriction
    - Consent management
    - Data retention policies
    """
    
    # Data retention periods (days)
    RETENTION_PERIODS = {
        "transactions": 2555,  # 7 years for financial records
        "invoices": 2555,      # 7 years
        "audit_logs": 365,     # 1 year
        "usage_data": 90,      # 90 days
        "session_data": 30,    # 30 days
    }
    
    async def export_user_data(
        self,
        user_id: str,
        db: AsyncSession,
        include_categories: Optional[List[str]] = None,
    ) -> DataExport:
        """
        Export all user data (GDPR Right to Access).
        
        Args:
            user_id: User ID
            db: Database session
            include_categories: Optional list of categories to include
            
        Returns:
            DataExport with all user data
        """
        from .models import CreditBalance, CreditTransaction, Invoice, Subscription
        from .economic_state import UserEconomicState
        
        categories = include_categories or [
            "profile", "credits", "transactions", "subscriptions", "invoices"
        ]
        
        data = {}
        
        # Economic state / profile
        if "profile" in categories:
            result = await db.execute(
                select(UserEconomicState).where(UserEconomicState.user_id == user_id)
            )
            state = result.scalar_one_or_none()
            if state:
                data["profile"] = {
                    "user_id": state.user_id,
                    "subscription_tier": str(state.subscription_tier),
                    "credit_balance": state.credit_balance,
                    "created_at": state.created_at.isoformat() if state.created_at else None,
                }
        
        # Credit balance
        if "credits" in categories:
            result = await db.execute(
                select(CreditBalance).where(CreditBalance.user_id == user_id)
            )
            balance = result.scalar_one_or_none()
            if balance:
                data["credits"] = {
                    "balance": balance.balance,
                    "lifetime_earned": balance.lifetime_earned,
                    "lifetime_used": balance.lifetime_used,
                }
        
        # Transactions
        if "transactions" in categories:
            result = await db.execute(
                select(CreditTransaction)
                .where(CreditTransaction.user_id == user_id)
                .order_by(CreditTransaction.created_at.desc())
            )
            transactions = result.scalars().all()
            data["transactions"] = [
                {
                    "id": str(tx.id),
                    "type": tx.tx_type,
                    "amount": tx.amount,
                    "balance_after": tx.balance_after,
                    "description": tx.description,
                    "created_at": tx.created_at.isoformat() if tx.created_at else None,
                }
                for tx in transactions
            ]
        
        # Subscriptions
        if "subscriptions" in categories:
            result = await db.execute(
                select(Subscription).where(Subscription.user_id == user_id)
            )
            subscriptions = result.scalars().all()
            data["subscriptions"] = [
                {
                    "id": str(sub.id),
                    "plan_id": sub.plan_id,
                    "status": sub.status,
                    "current_period_start": sub.current_period_start.isoformat() if sub.current_period_start else None,
                    "current_period_end": sub.current_period_end.isoformat() if sub.current_period_end else None,
                }
                for sub in subscriptions
            ]
        
        # Invoices
        if "invoices" in categories:
            result = await db.execute(
                select(Invoice).where(Invoice.user_id == user_id)
            )
            invoices = result.scalars().all()
            data["invoices"] = [
                {
                    "id": str(inv.id),
                    "invoice_number": inv.invoice_number,
                    "status": inv.status,
                    "total": float(inv.total) if inv.total else 0,
                    "created_at": inv.created_at.isoformat() if inv.created_at else None,
                }
                for inv in invoices
            ]
        
        logger.info(f"Exported data for user {user_id[:8]}...: {len(categories)} categories")
        
        return DataExport(
            user_id=user_id,
            export_date=datetime.utcnow().isoformat(),
            data_categories=categories,
            data=data,
        )
    
    async def delete_user_data(
        self,
        user_id: str,
        db: AsyncSession,
        retain_financial: bool = True,
    ) -> Dict[str, Any]:
        """
        Delete user data (GDPR Right to Erasure).
        
        Note: Financial records may need to be retained for legal compliance.
        
        Args:
            user_id: User ID
            db: Database session
            retain_financial: Whether to retain financial records
            
        Returns:
            Deletion summary
        """
        from .models import CreditBalance, CreditTransaction, Invoice, Subscription
        from .economic_state import UserEconomicState
        
        deleted = {}
        
        # Delete economic state
        result = await db.execute(
            delete(UserEconomicState).where(UserEconomicState.user_id == user_id)
        )
        deleted["economic_state"] = result.rowcount
        
        # Delete credit balance
        result = await db.execute(
            delete(CreditBalance).where(CreditBalance.user_id == user_id)
        )
        deleted["credit_balance"] = result.rowcount
        
        if not retain_financial:
            # Delete transactions (normally retained for 7 years)
            result = await db.execute(
                delete(CreditTransaction).where(CreditTransaction.user_id == user_id)
            )
            deleted["transactions"] = result.rowcount
            
            # Delete invoices
            result = await db.execute(
                delete(Invoice).where(Invoice.user_id == user_id)
            )
            deleted["invoices"] = result.rowcount
        else:
            # Anonymize financial records instead
            await self._anonymize_financial_records(user_id, db)
            deleted["transactions"] = "anonymized"
            deleted["invoices"] = "anonymized"
        
        # Delete subscriptions
        result = await db.execute(
            delete(Subscription).where(Subscription.user_id == user_id)
        )
        deleted["subscriptions"] = result.rowcount
        
        await db.commit()
        
        logger.info(f"Deleted data for user {user_id[:8]}...: {deleted}")
        
        return {
            "user_id": user_id,
            "deleted_at": datetime.utcnow().isoformat(),
            "deleted_records": deleted,
            "financial_retained": retain_financial,
        }
    
    async def _anonymize_financial_records(
        self,
        user_id: str,
        db: AsyncSession,
    ):
        """Anonymize financial records instead of deleting."""
        from .models import CreditTransaction, Invoice
        
        # Create anonymized user ID
        anon_id = f"anon_{hashlib.sha256(user_id.encode()).hexdigest()[:16]}"
        
        # Anonymize transactions
        await db.execute(
            update(CreditTransaction)
            .where(CreditTransaction.user_id == user_id)
            .values(
                user_id=anon_id,
                description="[ANONYMIZED]",
            )
        )
        
        # Anonymize invoices
        await db.execute(
            update(Invoice)
            .where(Invoice.user_id == user_id)
            .values(
                user_id=anon_id,
                billing_name="[ANONYMIZED]",
                billing_email="[ANONYMIZED]",
                billing_address=None,
            )
        )
    
    async def rectify_user_data(
        self,
        user_id: str,
        field: str,
        new_value: Any,
        db: AsyncSession,
    ) -> Dict[str, Any]:
        """
        Rectify user data (GDPR Right to Rectification).
        
        Args:
            user_id: User ID
            field: Field to rectify
            new_value: New value
            db: Database session
            
        Returns:
            Rectification result
        """
        from .economic_state import UserEconomicState
        
        # Only allow rectification of certain fields
        allowed_fields = ["billing_email", "billing_name", "billing_address"]
        
        if field not in allowed_fields:
            raise ValueError(f"Field '{field}' cannot be rectified")
        
        result = await db.execute(
            select(UserEconomicState).where(UserEconomicState.user_id == user_id)
        )
        state = result.scalar_one_or_none()
        
        if not state:
            raise ValueError(f"User {user_id} not found")
        
        old_value = getattr(state, field, None)
        setattr(state, field, new_value)
        
        await db.commit()
        
        logger.info(f"Rectified {field} for user {user_id[:8]}...")
        
        return {
            "user_id": user_id,
            "field": field,
            "old_value": old_value,
            "new_value": new_value,
            "rectified_at": datetime.utcnow().isoformat(),
        }
    
    async def apply_retention_policy(
        self,
        db: AsyncSession,
    ) -> Dict[str, int]:
        """
        Apply data retention policies.
        
        Should be run periodically (e.g., daily cron job).
        
        Args:
            db: Database session
            
        Returns:
            Count of deleted records by type
        """
        from .models import CreditTransaction
        from .audit_logger import AuditLog
        
        deleted = {}
        now = datetime.utcnow()
        
        # Delete old audit logs
        audit_cutoff = now - timedelta(days=self.RETENTION_PERIODS["audit_logs"])
        result = await db.execute(
            delete(AuditLog).where(AuditLog.created_at < audit_cutoff)
        )
        deleted["audit_logs"] = result.rowcount
        
        # Note: Transactions and invoices are retained for 7 years
        # and should not be auto-deleted
        
        await db.commit()
        
        logger.info(f"Applied retention policy: {deleted}")
        
        return deleted
    
    async def get_consent_status(
        self,
        user_id: str,
        db: AsyncSession,
    ) -> Dict[str, Any]:
        """
        Get user's consent status.
        
        Args:
            user_id: User ID
            db: Database session
            
        Returns:
            Consent status
        """
        from .economic_state import UserEconomicState
        
        result = await db.execute(
            select(UserEconomicState).where(UserEconomicState.user_id == user_id)
        )
        state = result.scalar_one_or_none()
        
        if not state:
            return {"user_id": user_id, "found": False}
        
        # Consent would typically be stored in user profile
        # This is a placeholder structure
        return {
            "user_id": user_id,
            "found": True,
            "consents": {
                "billing_processing": True,  # Required for service
                "marketing_emails": False,   # Optional
                "usage_analytics": True,     # Required for billing
            },
            "last_updated": state.updated_at.isoformat() if state.updated_at else None,
        }
    
    async def generate_privacy_report(
        self,
        user_id: str,
        db: AsyncSession,
    ) -> Dict[str, Any]:
        """
        Generate a privacy report for a user.
        
        Args:
            user_id: User ID
            db: Database session
            
        Returns:
            Privacy report
        """
        from .models import CreditTransaction, Invoice
        
        # Count data by category
        tx_result = await db.execute(
            select(CreditTransaction)
            .where(CreditTransaction.user_id == user_id)
        )
        tx_count = len(tx_result.scalars().all())
        
        inv_result = await db.execute(
            select(Invoice).where(Invoice.user_id == user_id)
        )
        inv_count = len(inv_result.scalars().all())
        
        return {
            "user_id": user_id,
            "report_date": datetime.utcnow().isoformat(),
            "data_summary": {
                "transactions": tx_count,
                "invoices": inv_count,
            },
            "retention_info": {
                "transactions": f"{self.RETENTION_PERIODS['transactions']} days",
                "invoices": f"{self.RETENTION_PERIODS['invoices']} days",
                "audit_logs": f"{self.RETENTION_PERIODS['audit_logs']} days",
            },
            "rights": {
                "access": "You can request a copy of all your data",
                "erasure": "You can request deletion of your data",
                "rectification": "You can request correction of inaccurate data",
                "portability": "You can request your data in a portable format",
            },
        }


# Global instance
compliance_service = ComplianceService()


# ============================================
# CONVENIENCE FUNCTIONS
# ============================================

async def export_user_data(user_id: str, db: AsyncSession) -> DataExport:
    """Export user data for GDPR compliance."""
    return await compliance_service.export_user_data(user_id, db)


async def delete_user_data(user_id: str, db: AsyncSession, retain_financial: bool = True):
    """Delete user data for GDPR compliance."""
    return await compliance_service.delete_user_data(user_id, db, retain_financial)
