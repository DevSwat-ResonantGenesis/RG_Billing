"""
Usage Analytics & Reports - Phase 3.2 GTM

Comprehensive usage analytics for billing insights.
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, asdict
from enum import Enum

from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class ReportPeriod(str, Enum):
    """Report time periods."""
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    YEARLY = "yearly"


class TrendDirection(str, Enum):
    """Usage trend direction."""
    INCREASING = "increasing"
    STABLE = "stable"
    DECREASING = "decreasing"


@dataclass
class UsageSummary:
    """Usage summary for a period."""
    period_start: datetime
    period_end: datetime
    total_credits_used: int
    total_transactions: int
    avg_daily_usage: float
    peak_day: Optional[str]
    peak_usage: int
    most_used_service: Optional[str]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "total_credits_used": self.total_credits_used,
            "total_transactions": self.total_transactions,
            "avg_daily_usage": round(self.avg_daily_usage, 2),
            "peak_day": self.peak_day,
            "peak_usage": self.peak_usage,
            "most_used_service": self.most_used_service,
        }


@dataclass
class ServiceBreakdown:
    """Usage breakdown by service."""
    service: str
    credits: int
    percentage: float
    transaction_count: int
    avg_per_transaction: float
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class UsageTrend:
    """Usage trend analysis."""
    direction: str
    percent_change: float
    current_period: int
    previous_period: int
    projected_monthly: int
    days_until_exhausted: int
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class UsageAnalytics:
    """
    Comprehensive usage analytics service.
    
    Features:
    - Period-based usage summaries
    - Service breakdown analysis
    - Trend detection
    - Projections and forecasting
    - Anomaly detection
    """
    
    async def get_usage_summary(
        self,
        user_id: str,
        period: ReportPeriod = ReportPeriod.MONTHLY,
        db: AsyncSession = None,
    ) -> UsageSummary:
        """
        Get usage summary for a period.
        
        Args:
            user_id: User ID
            period: Report period
            db: Database session
            
        Returns:
            UsageSummary with aggregated data
        """
        from .models import CreditTransaction
        
        # Calculate period dates
        now = datetime.utcnow()
        period_start, period_end = self._get_period_dates(now, period)
        days_in_period = (period_end - period_start).days or 1
        
        # Get total usage
        result = await db.execute(
            select(
                func.sum(func.abs(CreditTransaction.amount)).label("total"),
                func.count().label("count"),
            )
            .where(
                CreditTransaction.user_id == user_id,
                CreditTransaction.tx_type == "usage",
                CreditTransaction.created_at >= period_start,
                CreditTransaction.created_at <= period_end,
            )
        )
        row = result.one()
        total_credits = int(row.total or 0)
        total_transactions = row.count or 0
        
        # Get daily breakdown for peak detection
        daily_result = await db.execute(
            select(
                func.date(CreditTransaction.created_at).label("day"),
                func.sum(func.abs(CreditTransaction.amount)).label("daily_total"),
            )
            .where(
                CreditTransaction.user_id == user_id,
                CreditTransaction.tx_type == "usage",
                CreditTransaction.created_at >= period_start,
                CreditTransaction.created_at <= period_end,
            )
            .group_by(func.date(CreditTransaction.created_at))
            .order_by(func.sum(func.abs(CreditTransaction.amount)).desc())
        )
        daily_rows = daily_result.all()
        
        peak_day = None
        peak_usage = 0
        if daily_rows:
            peak_day = str(daily_rows[0].day)
            peak_usage = int(daily_rows[0].daily_total or 0)
        
        # Get most used service
        service_result = await db.execute(
            select(
                CreditTransaction.reference_type,
                func.sum(func.abs(CreditTransaction.amount)).label("total"),
            )
            .where(
                CreditTransaction.user_id == user_id,
                CreditTransaction.tx_type == "usage",
                CreditTransaction.created_at >= period_start,
                CreditTransaction.created_at <= period_end,
            )
            .group_by(CreditTransaction.reference_type)
            .order_by(func.sum(func.abs(CreditTransaction.amount)).desc())
            .limit(1)
        )
        service_row = service_result.first()
        most_used_service = service_row.reference_type if service_row else None
        
        return UsageSummary(
            period_start=period_start,
            period_end=period_end,
            total_credits_used=total_credits,
            total_transactions=total_transactions,
            avg_daily_usage=total_credits / days_in_period,
            peak_day=peak_day,
            peak_usage=peak_usage,
            most_used_service=most_used_service,
        )
    
    async def get_service_breakdown(
        self,
        user_id: str,
        period: ReportPeriod = ReportPeriod.MONTHLY,
        db: AsyncSession = None,
    ) -> List[ServiceBreakdown]:
        """
        Get usage breakdown by service.
        
        Args:
            user_id: User ID
            period: Report period
            db: Database session
            
        Returns:
            List of ServiceBreakdown by service
        """
        from .models import CreditTransaction
        
        now = datetime.utcnow()
        period_start, period_end = self._get_period_dates(now, period)
        
        # Get breakdown by service
        result = await db.execute(
            select(
                CreditTransaction.reference_type,
                func.sum(func.abs(CreditTransaction.amount)).label("total"),
                func.count().label("count"),
            )
            .where(
                CreditTransaction.user_id == user_id,
                CreditTransaction.tx_type == "usage",
                CreditTransaction.created_at >= period_start,
                CreditTransaction.created_at <= period_end,
            )
            .group_by(CreditTransaction.reference_type)
            .order_by(func.sum(func.abs(CreditTransaction.amount)).desc())
        )
        rows = result.all()
        
        # Calculate total for percentages
        total = sum(int(row.total or 0) for row in rows)
        
        breakdowns = []
        for row in rows:
            credits = int(row.total or 0)
            count = row.count or 1
            breakdowns.append(ServiceBreakdown(
                service=row.reference_type or "other",
                credits=credits,
                percentage=round((credits / total * 100) if total > 0 else 0, 1),
                transaction_count=count,
                avg_per_transaction=round(credits / count, 2),
            ))
        
        return breakdowns
    
    async def get_usage_trend(
        self,
        user_id: str,
        current_balance: int,
        db: AsyncSession = None,
    ) -> UsageTrend:
        """
        Analyze usage trends.
        
        Args:
            user_id: User ID
            current_balance: Current credit balance
            db: Database session
            
        Returns:
            UsageTrend with analysis
        """
        from .models import CreditTransaction
        
        now = datetime.utcnow()
        
        # Current period (last 30 days)
        current_start = now - timedelta(days=30)
        
        # Previous period (30-60 days ago)
        previous_start = now - timedelta(days=60)
        previous_end = now - timedelta(days=30)
        
        # Get current period usage
        current_result = await db.execute(
            select(func.sum(func.abs(CreditTransaction.amount)))
            .where(
                CreditTransaction.user_id == user_id,
                CreditTransaction.tx_type == "usage",
                CreditTransaction.created_at >= current_start,
            )
        )
        current_usage = int(current_result.scalar() or 0)
        
        # Get previous period usage
        previous_result = await db.execute(
            select(func.sum(func.abs(CreditTransaction.amount)))
            .where(
                CreditTransaction.user_id == user_id,
                CreditTransaction.tx_type == "usage",
                CreditTransaction.created_at >= previous_start,
                CreditTransaction.created_at < previous_end,
            )
        )
        previous_usage = int(previous_result.scalar() or 0)
        
        # Calculate trend
        if previous_usage > 0:
            percent_change = ((current_usage - previous_usage) / previous_usage) * 100
        else:
            percent_change = 100 if current_usage > 0 else 0
        
        if percent_change > 10:
            direction = TrendDirection.INCREASING.value
        elif percent_change < -10:
            direction = TrendDirection.DECREASING.value
        else:
            direction = TrendDirection.STABLE.value
        
        # Project monthly usage
        daily_avg = current_usage / 30
        projected_monthly = int(daily_avg * 30)
        
        # Days until exhausted
        if daily_avg > 0 and current_balance > 0:
            days_until_exhausted = int(current_balance / daily_avg)
        else:
            days_until_exhausted = -1  # Unlimited or no usage
        
        return UsageTrend(
            direction=direction,
            percent_change=round(percent_change, 1),
            current_period=current_usage,
            previous_period=previous_usage,
            projected_monthly=projected_monthly,
            days_until_exhausted=days_until_exhausted,
        )
    
    async def get_daily_usage(
        self,
        user_id: str,
        days: int = 30,
        db: AsyncSession = None,
    ) -> List[Dict[str, Any]]:
        """
        Get daily usage data for charts.
        
        Args:
            user_id: User ID
            days: Number of days
            db: Database session
            
        Returns:
            List of daily usage data points
        """
        from .models import CreditTransaction
        
        start_date = datetime.utcnow() - timedelta(days=days)
        
        result = await db.execute(
            select(
                func.date(CreditTransaction.created_at).label("date"),
                func.sum(func.abs(CreditTransaction.amount)).label("credits"),
                func.count().label("transactions"),
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
        
        return [
            {
                "date": str(row.date),
                "credits": int(row.credits or 0),
                "transactions": row.transactions,
            }
            for row in rows
        ]
    
    async def get_hourly_pattern(
        self,
        user_id: str,
        days: int = 7,
        db: AsyncSession = None,
    ) -> List[Dict[str, Any]]:
        """
        Get hourly usage pattern.
        
        Args:
            user_id: User ID
            days: Number of days to analyze
            db: Database session
            
        Returns:
            List of hourly usage averages
        """
        from .models import CreditTransaction
        
        start_date = datetime.utcnow() - timedelta(days=days)
        
        result = await db.execute(
            select(
                func.extract('hour', CreditTransaction.created_at).label("hour"),
                func.avg(func.abs(CreditTransaction.amount)).label("avg_credits"),
                func.count().label("count"),
            )
            .where(
                CreditTransaction.user_id == user_id,
                CreditTransaction.tx_type == "usage",
                CreditTransaction.created_at >= start_date,
            )
            .group_by(func.extract('hour', CreditTransaction.created_at))
            .order_by(func.extract('hour', CreditTransaction.created_at))
        )
        rows = result.all()
        
        return [
            {
                "hour": int(row.hour),
                "avg_credits": round(float(row.avg_credits or 0), 2),
                "transaction_count": row.count,
            }
            for row in rows
        ]
    
    async def detect_anomalies(
        self,
        user_id: str,
        threshold_multiplier: float = 2.0,
        db: AsyncSession = None,
    ) -> List[Dict[str, Any]]:
        """
        Detect usage anomalies.
        
        Args:
            user_id: User ID
            threshold_multiplier: Multiplier for anomaly threshold
            db: Database session
            
        Returns:
            List of anomalous days
        """
        from .models import CreditTransaction
        
        # Get last 30 days of daily usage
        start_date = datetime.utcnow() - timedelta(days=30)
        
        result = await db.execute(
            select(
                func.date(CreditTransaction.created_at).label("date"),
                func.sum(func.abs(CreditTransaction.amount)).label("credits"),
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
        
        if len(rows) < 7:
            return []  # Not enough data
        
        # Calculate mean and std
        values = [int(row.credits or 0) for row in rows]
        mean = sum(values) / len(values)
        variance = sum((x - mean) ** 2 for x in values) / len(values)
        std = variance ** 0.5
        
        threshold = mean + (std * threshold_multiplier)
        
        anomalies = []
        for row in rows:
            credits = int(row.credits or 0)
            if credits > threshold:
                anomalies.append({
                    "date": str(row.date),
                    "credits": credits,
                    "expected_max": int(threshold),
                    "deviation": round((credits - mean) / std if std > 0 else 0, 2),
                })
        
        return anomalies
    
    async def generate_report(
        self,
        user_id: str,
        period: ReportPeriod = ReportPeriod.MONTHLY,
        db: AsyncSession = None,
    ) -> Dict[str, Any]:
        """
        Generate comprehensive usage report.
        
        Args:
            user_id: User ID
            period: Report period
            db: Database session
            
        Returns:
            Complete usage report
        """
        from .economic_state import UserEconomicState
        
        # Get user's economic state
        result = await db.execute(
            select(UserEconomicState).where(UserEconomicState.user_id == user_id)
        )
        state = result.scalar_one_or_none()
        current_balance = state.credit_balance if state else 0
        
        # Gather all analytics
        summary = await self.get_usage_summary(user_id, period, db)
        breakdown = await self.get_service_breakdown(user_id, period, db)
        trend = await self.get_usage_trend(user_id, current_balance, db)
        daily = await self.get_daily_usage(user_id, 30, db)
        anomalies = await self.detect_anomalies(user_id, db=db)
        
        # Generate recommendations
        recommendations = self._generate_recommendations(
            summary, trend, breakdown, current_balance
        )
        
        return {
            "generated_at": datetime.utcnow().isoformat(),
            "user_id": user_id,
            "period": period.value,
            "summary": summary.to_dict(),
            "service_breakdown": [b.to_dict() for b in breakdown],
            "trend": trend.to_dict(),
            "daily_usage": daily,
            "anomalies": anomalies,
            "recommendations": recommendations,
            "current_balance": current_balance,
        }
    
    def _get_period_dates(
        self,
        reference: datetime,
        period: ReportPeriod,
    ) -> tuple:
        """Get start and end dates for a period."""
        if period == ReportPeriod.DAILY:
            start = reference.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)
        elif period == ReportPeriod.WEEKLY:
            start = reference - timedelta(days=reference.weekday())
            start = start.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=7)
        elif period == ReportPeriod.MONTHLY:
            start = reference.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            if reference.month == 12:
                end = start.replace(year=reference.year + 1, month=1)
            else:
                end = start.replace(month=reference.month + 1)
        elif period == ReportPeriod.QUARTERLY:
            quarter = (reference.month - 1) // 3
            start = reference.replace(month=quarter * 3 + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
            end_month = quarter * 3 + 4
            if end_month > 12:
                end = start.replace(year=reference.year + 1, month=end_month - 12)
            else:
                end = start.replace(month=end_month)
        else:  # YEARLY
            start = reference.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            end = start.replace(year=reference.year + 1)
        
        return start, end
    
    def _generate_recommendations(
        self,
        summary: UsageSummary,
        trend: UsageTrend,
        breakdown: List[ServiceBreakdown],
        current_balance: int,
    ) -> List[Dict[str, Any]]:
        """Generate usage recommendations."""
        recommendations = []
        
        # Low balance warning
        if trend.days_until_exhausted > 0 and trend.days_until_exhausted < 7:
            recommendations.append({
                "type": "low_balance",
                "priority": "high",
                "message": f"Credits may run out in {trend.days_until_exhausted} days. Consider purchasing more credits.",
            })
        
        # Increasing usage
        if trend.direction == TrendDirection.INCREASING.value and trend.percent_change > 50:
            recommendations.append({
                "type": "usage_spike",
                "priority": "medium",
                "message": f"Usage increased {trend.percent_change:.0f}% from last period. Review your usage patterns.",
            })
        
        # High concentration in one service
        if breakdown and breakdown[0].percentage > 70:
            recommendations.append({
                "type": "service_concentration",
                "priority": "low",
                "message": f"{breakdown[0].service} accounts for {breakdown[0].percentage:.0f}% of usage. Consider optimizing this service.",
            })
        
        # Upgrade suggestion
        if trend.projected_monthly > current_balance * 0.9:
            recommendations.append({
                "type": "upgrade",
                "priority": "medium",
                "message": "Projected usage exceeds your balance. Consider upgrading your plan.",
            })
        
        return recommendations


# Global instance
usage_analytics = UsageAnalytics()
