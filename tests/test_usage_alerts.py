"""
Tests for Usage Alerts Service - Phase 2.2 GTM

Tests usage threshold detection and alert notifications.
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from app.usage_alerts import (
    UsageAlertService,
    UsageAlertLevel,
    AlertConfig,
    ALERT_CONFIGS,
    usage_alert_service,
    check_usage_alerts,
)


class TestUsageAlertLevel:
    """Test UsageAlertLevel enum."""
    
    def test_alert_levels_defined(self):
        """Test all alert levels are defined."""
        assert UsageAlertLevel.WARNING_80.value == "warning_80"
        assert UsageAlertLevel.CRITICAL_90.value == "critical_90"
        assert UsageAlertLevel.EXHAUSTED_100.value == "exhausted_100"
    
    def test_alert_thresholds(self):
        """Test alert thresholds are correct."""
        assert UsageAlertLevel.WARNING_80.threshold == 80
        assert UsageAlertLevel.CRITICAL_90.threshold == 90
        assert UsageAlertLevel.EXHAUSTED_100.threshold == 100


class TestAlertConfigs:
    """Test alert configurations."""
    
    def test_all_levels_have_config(self):
        """Test all alert levels have configuration."""
        for level in UsageAlertLevel:
            assert level in ALERT_CONFIGS
    
    def test_config_structure(self):
        """Test config has required fields."""
        for level, config in ALERT_CONFIGS.items():
            assert isinstance(config, AlertConfig)
            assert config.level == level
            assert config.subject
            assert config.template
            assert config.priority in ["low", "medium", "high", "critical"]


class TestUsageAlertService:
    """Test UsageAlertService class."""
    
    def setup_method(self):
        self.service = UsageAlertService()
    
    def test_calculate_usage_percent_normal(self):
        """Test normal usage calculation."""
        # 200 used out of 1000 = 20%
        percent = self.service.calculate_usage_percent(800, 1000)
        assert percent == 20.0
    
    def test_calculate_usage_percent_high(self):
        """Test high usage calculation."""
        # 900 used out of 1000 = 90%
        percent = self.service.calculate_usage_percent(100, 1000)
        assert percent == 90.0
    
    def test_calculate_usage_percent_exhausted(self):
        """Test exhausted calculation."""
        percent = self.service.calculate_usage_percent(0, 1000)
        assert percent == 100.0
    
    def test_calculate_usage_percent_over(self):
        """Test over-usage calculation."""
        # Negative balance = over 100%
        percent = self.service.calculate_usage_percent(-100, 1000)
        assert abs(percent - 110.0) < 0.01  # Float comparison
    
    def test_calculate_usage_percent_unlimited(self):
        """Test unlimited tier returns 0."""
        percent = self.service.calculate_usage_percent(1000, 0)
        assert percent == 0.0
        
        percent = self.service.calculate_usage_percent(1000, -1)
        assert percent == 0.0
    
    def test_get_triggered_level_none(self):
        """Test no alert triggered below 80%."""
        assert self.service.get_triggered_level(0) is None
        assert self.service.get_triggered_level(50) is None
        assert self.service.get_triggered_level(79.9) is None
    
    def test_get_triggered_level_warning(self):
        """Test warning at 80%."""
        assert self.service.get_triggered_level(80) == UsageAlertLevel.WARNING_80
        assert self.service.get_triggered_level(85) == UsageAlertLevel.WARNING_80
        assert self.service.get_triggered_level(89.9) == UsageAlertLevel.WARNING_80
    
    def test_get_triggered_level_critical(self):
        """Test critical at 90%."""
        assert self.service.get_triggered_level(90) == UsageAlertLevel.CRITICAL_90
        assert self.service.get_triggered_level(95) == UsageAlertLevel.CRITICAL_90
        assert self.service.get_triggered_level(99.9) == UsageAlertLevel.CRITICAL_90
    
    def test_get_triggered_level_exhausted(self):
        """Test exhausted at 100%."""
        assert self.service.get_triggered_level(100) == UsageAlertLevel.EXHAUSTED_100
        assert self.service.get_triggered_level(110) == UsageAlertLevel.EXHAUSTED_100
    
    @pytest.mark.asyncio
    async def test_check_and_alert_no_trigger(self):
        """Test no alert when usage is low."""
        result = await self.service.check_and_alert(
            user_id="user123",
            balance=800,  # 20% used
            tier_credits=1000,
        )
        assert result is None
    
    @pytest.mark.asyncio
    async def test_check_and_alert_unlimited(self):
        """Test no alert for unlimited tier."""
        result = await self.service.check_and_alert(
            user_id="user123",
            balance=100,
            tier_credits=0,  # Unlimited
        )
        assert result is None
    
    @pytest.mark.asyncio
    async def test_check_and_alert_triggers_warning(self):
        """Test warning alert is triggered."""
        with patch.object(self.service, '_send_alert', new_callable=AsyncMock):
            result = await self.service.check_and_alert(
                user_id="user123",
                balance=150,  # 85% used
                tier_credits=1000,
            )
        
        assert result == UsageAlertLevel.WARNING_80
    
    @pytest.mark.asyncio
    async def test_check_and_alert_deduplication(self):
        """Test alerts are deduplicated."""
        with patch.object(self.service, '_send_alert', new_callable=AsyncMock) as mock_send:
            # First call triggers alert
            result1 = await self.service.check_and_alert(
                user_id="user123",
                balance=150,
                tier_credits=1000,
            )
            
            # Second call should not trigger (already alerted)
            result2 = await self.service.check_and_alert(
                user_id="user123",
                balance=150,
                tier_credits=1000,
            )
        
        assert result1 == UsageAlertLevel.WARNING_80
        assert result2 is None
        assert mock_send.call_count == 1
    
    def test_reset_alerts(self):
        """Test resetting alerts for a user."""
        # Mark as alerted
        self.service._alerted["user123"] = {UsageAlertLevel.WARNING_80}
        
        # Reset
        self.service.reset_alerts("user123")
        
        assert "user123" not in self.service._alerted


class TestAlertDeduplication:
    """Test alert deduplication logic."""
    
    def setup_method(self):
        self.service = UsageAlertService()
    
    @pytest.mark.asyncio
    async def test_already_alerted_memory(self):
        """Test memory-based deduplication."""
        user_id = "user456"
        level = UsageAlertLevel.CRITICAL_90
        
        # Not alerted initially
        assert not await self.service._already_alerted(user_id, level)
        
        # Mark as alerted
        await self.service._mark_alerted(user_id, level)
        
        # Now should be alerted
        assert await self.service._already_alerted(user_id, level)
    
    @pytest.mark.asyncio
    async def test_different_levels_independent(self):
        """Test different alert levels are tracked independently."""
        user_id = "user789"
        
        await self.service._mark_alerted(user_id, UsageAlertLevel.WARNING_80)
        
        # Warning is alerted
        assert await self.service._already_alerted(user_id, UsageAlertLevel.WARNING_80)
        
        # Critical is not alerted
        assert not await self.service._already_alerted(user_id, UsageAlertLevel.CRITICAL_90)


class TestGlobalAlertService:
    """Test global alert service instance."""
    
    def test_global_instance_exists(self):
        """Test global instance exists."""
        assert usage_alert_service is not None
        assert isinstance(usage_alert_service, UsageAlertService)


class TestCheckUsageAlertsFunction:
    """Test convenience function."""
    
    @pytest.mark.asyncio
    async def test_check_usage_alerts(self):
        """Test check_usage_alerts convenience function."""
        with patch.object(usage_alert_service, 'check_and_alert', new_callable=AsyncMock) as mock:
            mock.return_value = UsageAlertLevel.WARNING_80
            
            result = await check_usage_alerts(
                user_id="user123",
                balance=150,
                tier_credits=1000,
            )
        
        assert result == UsageAlertLevel.WARNING_80
        mock.assert_called_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
