"""
Team Billing & Cost Allocation - Phase 3.4 GTM

Manage billing for teams/organizations with member budgets.
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, asdict
from decimal import Decimal

from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, Boolean, select, func, update
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func as sql_func
import uuid

from .db import Base

logger = logging.getLogger(__name__)


class Organization(Base):
    """Organization/Team for billing purposes."""
    __tablename__ = "organizations"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False)
    owner_id = Column(String(64), nullable=False, index=True)
    
    # Billing settings
    billing_email = Column(String(255))
    billing_address = Column(JSONB)
    stripe_customer_id = Column(String(64))
    
    # Credit pool
    credit_pool = Column(Integer, default=0)
    monthly_budget = Column(Integer, default=0)  # 0 = unlimited
    
    # Settings
    allow_member_overage = Column(Boolean, default=False)
    require_budget_approval = Column(Boolean, default=False)
    
    created_at = Column(DateTime(timezone=True), server_default=sql_func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=sql_func.now())


class OrganizationMember(Base):
    """Organization member with budget allocation."""
    __tablename__ = "organization_members"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True)
    user_id = Column(String(64), nullable=False, index=True)
    
    # Role
    role = Column(String(32), default="member")  # owner, admin, member
    
    # Budget allocation
    monthly_budget = Column(Integer, default=0)  # 0 = use org pool
    budget_used = Column(Integer, default=0)
    budget_period_start = Column(DateTime(timezone=True))
    
    # Permissions
    can_view_org_usage = Column(Boolean, default=False)
    can_manage_members = Column(Boolean, default=False)
    
    created_at = Column(DateTime(timezone=True), server_default=sql_func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=sql_func.now())


@dataclass
class MemberUsage:
    """Usage data for a team member."""
    user_id: str
    name: str
    email: str
    role: str
    credits_used: int
    percentage: float
    monthly_budget: int
    budget_remaining: int
    transaction_count: int
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TeamUsageSummary:
    """Team usage summary."""
    org_id: str
    org_name: str
    total_credits_used: int
    member_count: int
    credit_pool: int
    monthly_budget: int
    budget_remaining: int
    top_users: List[MemberUsage]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "org_id": self.org_id,
            "org_name": self.org_name,
            "total_credits_used": self.total_credits_used,
            "member_count": self.member_count,
            "credit_pool": self.credit_pool,
            "monthly_budget": self.monthly_budget,
            "budget_remaining": self.budget_remaining,
            "top_users": [u.to_dict() for u in self.top_users],
        }


class TeamBillingService:
    """
    Manage billing for teams/organizations.
    
    Features:
    - Organization credit pools
    - Per-member budget allocation
    - Usage tracking by member
    - Cost allocation reports
    - Budget enforcement
    """
    
    async def create_organization(
        self,
        name: str,
        owner_id: str,
        billing_email: Optional[str] = None,
        monthly_budget: int = 0,
        db: AsyncSession = None,
    ) -> Organization:
        """
        Create a new organization.
        
        Args:
            name: Organization name
            owner_id: Owner user ID
            billing_email: Billing email
            monthly_budget: Monthly budget (0 = unlimited)
            db: Database session
            
        Returns:
            Created Organization
        """
        org = Organization(
            name=name,
            owner_id=owner_id,
            billing_email=billing_email,
            monthly_budget=monthly_budget,
        )
        db.add(org)
        
        # Add owner as member
        member = OrganizationMember(
            org_id=org.id,
            user_id=owner_id,
            role="owner",
            can_view_org_usage=True,
            can_manage_members=True,
        )
        db.add(member)
        
        await db.commit()
        await db.refresh(org)
        
        logger.info(f"Created organization {org.id}: {name}")
        return org
    
    async def add_member(
        self,
        org_id: str,
        user_id: str,
        role: str = "member",
        monthly_budget: int = 0,
        db: AsyncSession = None,
    ) -> OrganizationMember:
        """
        Add a member to an organization.
        
        Args:
            org_id: Organization ID
            user_id: User ID to add
            role: Member role (owner, admin, member)
            monthly_budget: Monthly budget allocation
            db: Database session
            
        Returns:
            Created OrganizationMember
        """
        member = OrganizationMember(
            org_id=org_id,
            user_id=user_id,
            role=role,
            monthly_budget=monthly_budget,
            budget_period_start=datetime.utcnow(),
            can_view_org_usage=role in ["owner", "admin"],
            can_manage_members=role == "owner",
        )
        db.add(member)
        await db.commit()
        await db.refresh(member)
        
        logger.info(f"Added member {user_id} to org {org_id}")
        return member
    
    async def set_member_budget(
        self,
        org_id: str,
        user_id: str,
        monthly_budget: int,
        db: AsyncSession = None,
    ) -> OrganizationMember:
        """
        Set monthly budget for a team member.
        
        Args:
            org_id: Organization ID
            user_id: User ID
            monthly_budget: Monthly budget (0 = use org pool)
            db: Database session
            
        Returns:
            Updated OrganizationMember
        """
        result = await db.execute(
            select(OrganizationMember)
            .where(
                OrganizationMember.org_id == org_id,
                OrganizationMember.user_id == user_id,
            )
        )
        member = result.scalar_one_or_none()
        
        if not member:
            raise ValueError(f"Member {user_id} not found in org {org_id}")
        
        member.monthly_budget = monthly_budget
        await db.commit()
        await db.refresh(member)
        
        logger.info(f"Set budget for {user_id} in org {org_id}: {monthly_budget}")
        return member
    
    async def check_member_budget(
        self,
        org_id: str,
        user_id: str,
        amount: int,
        db: AsyncSession = None,
    ) -> Dict[str, Any]:
        """
        Check if member has budget for an operation.
        
        Args:
            org_id: Organization ID
            user_id: User ID
            amount: Credits needed
            db: Database session
            
        Returns:
            Dict with allowed status and details
        """
        result = await db.execute(
            select(OrganizationMember)
            .where(
                OrganizationMember.org_id == org_id,
                OrganizationMember.user_id == user_id,
            )
        )
        member = result.scalar_one_or_none()
        
        if not member:
            return {"allowed": False, "reason": "not_member"}
        
        # Get org for pool check
        org_result = await db.execute(
            select(Organization).where(Organization.id == org_id)
        )
        org = org_result.scalar_one_or_none()
        
        if not org:
            return {"allowed": False, "reason": "org_not_found"}
        
        # Check member budget if set
        if member.monthly_budget > 0:
            remaining = member.monthly_budget - member.budget_used
            if amount > remaining:
                if not org.allow_member_overage:
                    return {
                        "allowed": False,
                        "reason": "member_budget_exceeded",
                        "budget": member.monthly_budget,
                        "used": member.budget_used,
                        "remaining": remaining,
                        "requested": amount,
                    }
        
        # Check org pool
        if org.monthly_budget > 0:
            # Calculate total org usage this period
            period_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            
            from .models import CreditTransaction
            
            # Get all member user IDs
            members_result = await db.execute(
                select(OrganizationMember.user_id)
                .where(OrganizationMember.org_id == org_id)
            )
            member_ids = [r[0] for r in members_result.all()]
            
            usage_result = await db.execute(
                select(func.sum(func.abs(CreditTransaction.amount)))
                .where(
                    CreditTransaction.user_id.in_(member_ids),
                    CreditTransaction.tx_type == "usage",
                    CreditTransaction.created_at >= period_start,
                )
            )
            total_usage = int(usage_result.scalar() or 0)
            
            remaining = org.monthly_budget - total_usage
            if amount > remaining:
                return {
                    "allowed": False,
                    "reason": "org_budget_exceeded",
                    "budget": org.monthly_budget,
                    "used": total_usage,
                    "remaining": remaining,
                    "requested": amount,
                }
        
        return {
            "allowed": True,
            "member_budget": member.monthly_budget,
            "member_used": member.budget_used,
            "org_pool": org.credit_pool,
        }
    
    async def record_member_usage(
        self,
        org_id: str,
        user_id: str,
        amount: int,
        db: AsyncSession = None,
    ):
        """
        Record usage against member's budget.
        
        Args:
            org_id: Organization ID
            user_id: User ID
            amount: Credits used
            db: Database session
        """
        await db.execute(
            update(OrganizationMember)
            .where(
                OrganizationMember.org_id == org_id,
                OrganizationMember.user_id == user_id,
            )
            .values(budget_used=OrganizationMember.budget_used + amount)
        )
        await db.commit()
    
    async def get_team_usage(
        self,
        org_id: str,
        db: AsyncSession = None,
    ) -> TeamUsageSummary:
        """
        Get usage breakdown by team member.
        
        Args:
            org_id: Organization ID
            db: Database session
            
        Returns:
            TeamUsageSummary with member breakdown
        """
        from .models import CreditTransaction
        
        # Get organization
        org_result = await db.execute(
            select(Organization).where(Organization.id == org_id)
        )
        org = org_result.scalar_one_or_none()
        
        if not org:
            raise ValueError(f"Organization {org_id} not found")
        
        # Get members
        members_result = await db.execute(
            select(OrganizationMember)
            .where(OrganizationMember.org_id == org_id)
        )
        members = members_result.scalars().all()
        
        # Get usage for each member
        period_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        
        member_usage = []
        total_usage = 0
        
        for member in members:
            usage_result = await db.execute(
                select(
                    func.sum(func.abs(CreditTransaction.amount)).label("total"),
                    func.count().label("count"),
                )
                .where(
                    CreditTransaction.user_id == member.user_id,
                    CreditTransaction.tx_type == "usage",
                    CreditTransaction.created_at >= period_start,
                )
            )
            row = usage_result.one()
            credits_used = int(row.total or 0)
            total_usage += credits_used
            
            member_usage.append({
                "user_id": member.user_id,
                "role": member.role,
                "credits_used": credits_used,
                "monthly_budget": member.monthly_budget,
                "budget_used": member.budget_used,
                "transaction_count": row.count or 0,
            })
        
        # Calculate percentages and create MemberUsage objects
        top_users = []
        for m in sorted(member_usage, key=lambda x: x["credits_used"], reverse=True):
            percentage = (m["credits_used"] / total_usage * 100) if total_usage > 0 else 0
            budget_remaining = m["monthly_budget"] - m["budget_used"] if m["monthly_budget"] > 0 else -1
            
            top_users.append(MemberUsage(
                user_id=m["user_id"],
                name="",  # Would be fetched from user service
                email="",
                role=m["role"],
                credits_used=m["credits_used"],
                percentage=round(percentage, 1),
                monthly_budget=m["monthly_budget"],
                budget_remaining=budget_remaining,
                transaction_count=m["transaction_count"],
            ))
        
        # Calculate org budget remaining
        budget_remaining = org.monthly_budget - total_usage if org.monthly_budget > 0 else -1
        
        return TeamUsageSummary(
            org_id=str(org.id),
            org_name=org.name,
            total_credits_used=total_usage,
            member_count=len(members),
            credit_pool=org.credit_pool,
            monthly_budget=org.monthly_budget,
            budget_remaining=budget_remaining,
            top_users=top_users,
        )
    
    async def get_cost_allocation_report(
        self,
        org_id: str,
        period_start: Optional[datetime] = None,
        period_end: Optional[datetime] = None,
        db: AsyncSession = None,
    ) -> Dict[str, Any]:
        """
        Generate cost allocation report for an organization.
        
        Args:
            org_id: Organization ID
            period_start: Report start date
            period_end: Report end date
            db: Database session
            
        Returns:
            Cost allocation report
        """
        from .models import CreditTransaction
        
        if not period_start:
            period_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if not period_end:
            period_end = datetime.utcnow()
        
        # Get organization
        org_result = await db.execute(
            select(Organization).where(Organization.id == org_id)
        )
        org = org_result.scalar_one_or_none()
        
        if not org:
            raise ValueError(f"Organization {org_id} not found")
        
        # Get members
        members_result = await db.execute(
            select(OrganizationMember)
            .where(OrganizationMember.org_id == org_id)
        )
        members = {m.user_id: m for m in members_result.scalars().all()}
        
        # Get usage by member and service
        member_ids = list(members.keys())
        
        result = await db.execute(
            select(
                CreditTransaction.user_id,
                CreditTransaction.reference_type,
                func.sum(func.abs(CreditTransaction.amount)).label("total"),
                func.count().label("count"),
            )
            .where(
                CreditTransaction.user_id.in_(member_ids),
                CreditTransaction.tx_type == "usage",
                CreditTransaction.created_at >= period_start,
                CreditTransaction.created_at <= period_end,
            )
            .group_by(CreditTransaction.user_id, CreditTransaction.reference_type)
        )
        rows = result.all()
        
        # Build allocation data
        by_member = {}
        by_service = {}
        total = 0
        
        for row in rows:
            user_id = row.user_id
            service = row.reference_type or "other"
            credits = int(row.total or 0)
            total += credits
            
            # By member
            if user_id not in by_member:
                by_member[user_id] = {
                    "user_id": user_id,
                    "role": members[user_id].role if user_id in members else "unknown",
                    "total": 0,
                    "services": {},
                }
            by_member[user_id]["total"] += credits
            by_member[user_id]["services"][service] = by_member[user_id]["services"].get(service, 0) + credits
            
            # By service
            if service not in by_service:
                by_service[service] = {"total": 0, "members": {}}
            by_service[service]["total"] += credits
            by_service[service]["members"][user_id] = by_service[service]["members"].get(user_id, 0) + credits
        
        # Calculate percentages
        for user_id, data in by_member.items():
            data["percentage"] = round((data["total"] / total * 100) if total > 0 else 0, 1)
        
        for service, data in by_service.items():
            data["percentage"] = round((data["total"] / total * 100) if total > 0 else 0, 1)
        
        # Credit value for cost calculation
        CREDIT_VALUE = 0.001  # $0.001 per credit
        
        return {
            "org_id": str(org.id),
            "org_name": org.name,
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "total_credits": total,
            "total_cost_usd": round(total * CREDIT_VALUE, 2),
            "member_count": len(members),
            "by_member": list(by_member.values()),
            "by_service": [
                {"service": k, **v}
                for k, v in sorted(by_service.items(), key=lambda x: x[1]["total"], reverse=True)
            ],
        }
    
    async def reset_member_budgets(
        self,
        org_id: str,
        db: AsyncSession = None,
    ):
        """
        Reset all member budgets for new period.
        
        Should be run at start of each billing period.
        
        Args:
            org_id: Organization ID
            db: Database session
        """
        await db.execute(
            update(OrganizationMember)
            .where(OrganizationMember.org_id == org_id)
            .values(
                budget_used=0,
                budget_period_start=datetime.utcnow(),
            )
        )
        await db.commit()
        
        logger.info(f"Reset member budgets for org {org_id}")


# Global instance
team_billing_service = TeamBillingService()
