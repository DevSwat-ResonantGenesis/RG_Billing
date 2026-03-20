"""
Tests for Usage Analytics Service - Phase 3.2 GTM

Tests usage analytics and reporting.
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from app.usage_analytics import (
    UsageAnalytics,
    UsageSummary,
    ServiceBreakdown,
    UsageTrend,
    ReportPeriod,
    TrendDirection,
    usage_analytics,
)


class TestReportPeriod:
    """Test ReportPeriod enum."""
    
    def test_period_values(self):
        """Test all period values."""
        assert ReportPeriod.DAILY.value == "daily"
        assert ReportPeriod.WEEKLY.value == "weekly"
        assert ReportPeriod.MONTHLY.value == "monthly"
        assert ReportPeriod.QUARTERLY.value == "quarterly"
        assert ReportPeriod.YEARLY.value == "yearly"


class TestTrendDirection:
    """Test TrendDirection enum."""
    
    def test_direction_values(self):
        """Test all direction values."""
        assert TrendDirection.INCREASING.value == "increasing"
        assert TrendDirection.STABLE.value == "stable"
        assert TrendDirection.DECREASING.value == "decreasing"


class TestUsageSummary:
    """Test UsageSummary dataclass."""
    
    def test_to_dict(self):
        """Test UsageSummary.to_dict()."""
        summary = UsageSummary(
            period_start=datetime(2024, 1, 1),
            period_end=datetime(2024, 1, 31),
            total_credits_used=10000,
            total_transactions=500,
            avg_daily_usage=333.33,
            peak_day="2024-01-15",
            peak_usage=1000,
            most_used_service="chat",
        )
        
        d = summary.to_dict()
        
        assert d["total_credits_used"] == 10000
        assert d["total_transactions"] == 500
        assert d["avg_daily_usage"] == 333.33
        assert d["peak_day"] == "2024-01-15"
        assert d["peak_usage"] == 1000
        assert d["most_used_service"] == "chat"


class TestServiceBreakdown:
    """Test ServiceBreakdown dataclass."""
    
    def test_to_dict(self):
        """Test ServiceBreakdown.to_dict()."""
        breakdown = ServiceBreakdown(
            service="chat",
            credits=5000,
            percentage=50.0,
            transaction_count=250,
            avg_per_transaction=20.0,
        )
        
        d = breakdown.to_dict()
        
        assert d["service"] == "chat"
        assert d["credits"] == 5000
        assert d["percentage"] == 50.0
        assert d["transaction_count"] == 250
        assert d["avg_per_transaction"] == 20.0


class TestUsageTrend:
    """Test UsageTrend dataclass."""
    
    def test_to_dict(self):
        """Test UsageTrend.to_dict()."""
        trend = UsageTrend(
            direction="increasing",
            percent_change=25.5,
            current_period=10000,
            previous_period=8000,
            projected_monthly=12000,
            days_until_exhausted=30,
        )
        
        d = trend.to_dict()
        
        assert d["direction"] == "increasing"
        assert d["percent_change"] == 25.5
        assert d["current_period"] == 10000
        assert d["previous_period"] == 8000
        assert d["projected_monthly"] == 12000
        assert d["days_until_exhausted"] == 30


class TestUsageAnalytics:
    """Test UsageAnalytics class."""
    
    def setup_method(self):
        self.analytics = UsageAnalytics()
    
    def test_get_period_dates_daily(self):
        """Test daily period dates."""
        ref = datetime(2024, 1, 15, 12, 30, 45)
        start, end = self.analytics._get_period_dates(ref, ReportPeriod.DAILY)
        
        assert start.day == 15
        assert start.hour == 0
        assert start.minute == 0
        assert (end - start).days == 1
    
    def test_get_period_dates_weekly(self):
        """Test weekly period dates."""
        ref = datetime(2024, 1, 15, 12, 30, 45)  # Monday
        start, end = self.analytics._get_period_dates(ref, ReportPeriod.WEEKLY)
        
        assert start.weekday() == 0  # Monday
        assert (end - start).days == 7
    
    def test_get_period_dates_monthly(self):
        """Test monthly period dates."""
        ref = datetime(2024, 1, 15, 12, 30, 45)
        start, end = self.analytics._get_period_dates(ref, ReportPeriod.MONTHLY)
        
        assert start.day == 1
        assert start.month == 1
        assert end.month == 2
    
    def test_get_period_dates_quarterly(self):
        """Test quarterly period dates."""
        ref = datetime(2024, 2, 15)  # Q1
        start, end = self.analytics._get_period_dates(ref, ReportPeriod.QUARTERLY)
        
        assert start.month == 1
        assert end.month == 4
    
    def test_get_period_dates_yearly(self):
        """Test yearly period dates."""
        ref = datetime(2024, 6, 15)
        start, end = self.analytics._get_period_dates(ref, ReportPeriod.YEARLY)
        
        assert start.month == 1
        assert start.day == 1
        assert end.year == 2025


class TestGenerateRecommendations:
    """Test recommendation generation."""
    
    def setup_method(self):
        self.analytics = UsageAnalytics()
    
    def test_low_balance_recommendation(self):
        """Test low balance generates recommendation."""
        summary = UsageSummary(
            period_start=datetime.utcnow(),
            period_end=datetime.utcnow(),
            total_credits_used=1000,
            total_transactions=50,
            avg_daily_usage=100,
            peak_day=None,
            peak_usage=0,
            most_used_service=None,
        )
        trend = UsageTrend(
            direction="stable",
            percent_change=0,
            current_period=1000,
            previous_period=1000,
            projected_monthly=3000,
            days_until_exhausted=5,  # Low!
        )
        breakdown = []
        
        recs = self.analytics._generate_recommendations(summary, trend, breakdown, 500)
        
        assert any(r["type"] == "low_balance" for r in recs)
    
    def test_usage_spike_recommendation(self):
        """Test usage spike generates recommendation."""
        summary = UsageSummary(
            period_start=datetime.utcnow(),
            period_end=datetime.utcnow(),
            total_credits_used=10000,
            total_transactions=500,
            avg_daily_usage=333,
            peak_day=None,
            peak_usage=0,
            most_used_service=None,
        )
        trend = UsageTrend(
            direction="increasing",
            percent_change=75,  # High increase!
            current_period=10000,
            previous_period=5000,
            projected_monthly=10000,
            days_until_exhausted=30,
        )
        breakdown = []
        
        recs = self.analytics._generate_recommendations(summary, trend, breakdown, 50000)
        
        assert any(r["type"] == "usage_spike" for r in recs)
    
    def test_service_concentration_recommendation(self):
        """Test service concentration generates recommendation."""
        summary = UsageSummary(
            period_start=datetime.utcnow(),
            period_end=datetime.utcnow(),
            total_credits_used=10000,
            total_transactions=500,
            avg_daily_usage=333,
            peak_day=None,
            peak_usage=0,
            most_used_service="chat",
        )
        trend = UsageTrend(
            direction="stable",
            percent_change=0,
            current_period=10000,
            previous_period=10000,
            projected_monthly=10000,
            days_until_exhausted=30,
        )
        breakdown = [
            ServiceBreakdown(
                service="chat",
                credits=8000,
                percentage=80,  # High concentration!
                transaction_count=400,
                avg_per_transaction=20,
            ),
        ]
        
        recs = self.analytics._generate_recommendations(summary, trend, breakdown, 50000)
        
        assert any(r["type"] == "service_concentration" for r in recs)


class TestGlobalInstance:
    """Test global instance."""
    
    def test_global_instance_exists(self):
        """Test global instance exists."""
        assert usage_analytics is not None
        assert isinstance(usage_analytics, UsageAnalytics)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
