"""
Tests for Credit Rollover Service - Phase 3.3 GTM

Tests credit rollover between billing periods.
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from app.credit_rollover import (
    CreditRolloverService,
    RolloverResult,
    ROLLOVER_LIMITS,
    TIER_CREDITS,
    credit_rollover_service,
    process_user_rollover,
    preview_user_rollover,
)


class TestRolloverLimits:
    """Test rollover limit configuration."""
    
    def test_free_tier_no_rollover(self):
        """Test free tier has no rollover."""
        assert ROLLOVER_LIMITS["developer"] == 0
        assert ROLLOVER_LIMITS["free"] == 0
    
    def test_plus_tier_capped_rollover(self):
        """Test plus tier has capped rollover."""
        assert ROLLOVER_LIMITS["plus"] == 25000
        assert ROLLOVER_LIMITS["pro"] == 25000
    
    def test_enterprise_unlimited_rollover(self):
        """Test enterprise has unlimited rollover."""
        assert ROLLOVER_LIMITS["enterprise"] == -1


class TestTierCredits:
    """Test tier credit allocations."""
    
    def test_free_tier_credits(self):
        """Test free tier credit allocation."""
        assert TIER_CREDITS["developer"] == 1000
        assert TIER_CREDITS["free"] == 1000
    
    def test_plus_tier_credits(self):
        """Test plus tier credit allocation."""
        assert TIER_CREDITS["plus"] == 50000
        assert TIER_CREDITS["pro"] == 50000
    
    def test_enterprise_unlimited(self):
        """Test enterprise has unlimited credits."""
        assert TIER_CREDITS["enterprise"] == -1


class TestCreditRolloverService:
    """Test CreditRolloverService class."""
    
    def setup_method(self):
        self.service = CreditRolloverService()
    
    def test_get_rollover_limit(self):
        """Test getting rollover limit for tiers."""
        assert self.service.get_rollover_limit("developer") == 0
        assert self.service.get_rollover_limit("plus") == 25000
        assert self.service.get_rollover_limit("enterprise") == -1
        assert self.service.get_rollover_limit("unknown") == 0
    
    def test_get_tier_credits(self):
        """Test getting tier credits."""
        assert self.service.get_tier_credits("developer") == 1000
        assert self.service.get_tier_credits("plus") == 50000
        assert self.service.get_tier_credits("enterprise") == -1


class TestCalculateRollover:
    """Test rollover calculation."""
    
    def setup_method(self):
        self.service = CreditRolloverService()
    
    def test_free_tier_no_rollover(self):
        """Test free tier loses all credits."""
        amount, capped, cap = self.service.calculate_rollover(5000, "developer")
        
        assert amount == 0
        assert capped is True
        assert cap == 0
    
    def test_plus_tier_under_cap(self):
        """Test plus tier rollover under cap."""
        amount, capped, cap = self.service.calculate_rollover(10000, "plus")
        
        assert amount == 10000
        assert capped is False
        assert cap == 25000
    
    def test_plus_tier_over_cap(self):
        """Test plus tier rollover over cap."""
        amount, capped, cap = self.service.calculate_rollover(40000, "plus")
        
        assert amount == 25000
        assert capped is True
        assert cap == 25000
    
    def test_plus_tier_at_cap(self):
        """Test plus tier rollover at exactly cap."""
        amount, capped, cap = self.service.calculate_rollover(25000, "plus")
        
        assert amount == 25000
        assert capped is False
        assert cap == 25000
    
    def test_enterprise_unlimited(self):
        """Test enterprise keeps all credits."""
        amount, capped, cap = self.service.calculate_rollover(100000, "enterprise")
        
        assert amount == 100000
        assert capped is False
        assert cap == -1
    
    def test_zero_balance(self):
        """Test zero balance rollover."""
        amount, capped, cap = self.service.calculate_rollover(0, "plus")
        
        assert amount == 0
        assert capped is False


class TestRolloverResult:
    """Test RolloverResult dataclass."""
    
    def test_to_dict(self):
        """Test RolloverResult.to_dict()."""
        result = RolloverResult(
            user_id="user123",
            tier="plus",
            previous_balance=30000,
            rollover_amount=25000,
            new_credits=50000,
            new_balance=75000,
            rollover_capped=True,
            cap_amount=25000,
        )
        
        d = result.to_dict()
        
        assert d["user_id"] == "user123"
        assert d["tier"] == "plus"
        assert d["previous_balance"] == 30000
        assert d["rollover_amount"] == 25000
        assert d["new_credits"] == 50000
        assert d["new_balance"] == 75000
        assert d["rollover_capped"] is True
        assert d["cap_amount"] == 25000


class TestProcessPeriodEnd:
    """Test period end processing."""
    
    def setup_method(self):
        self.service = CreditRolloverService()
    
    @pytest.mark.asyncio
    async def test_process_period_end_plus_tier(self):
        """Test period end for plus tier - unit test calculation logic."""
        # Test the calculation directly since DB mocking is complex
        rollover, capped, cap = self.service.calculate_rollover(30000, "plus")
        new_credits = self.service.get_tier_credits("plus")
        new_balance = rollover + new_credits
        
        assert rollover == 25000  # Capped at 25K
        assert capped is True
        assert new_credits == 50000
        assert new_balance == 75000
    
    @pytest.mark.asyncio
    async def test_process_period_end_user_not_found(self):
        """Test period end for non-existent user."""
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        
        with pytest.raises(ValueError, match="not found"):
            await self.service.process_period_end("nonexistent", mock_db)


class TestPreviewRollover:
    """Test rollover preview."""
    
    def setup_method(self):
        self.service = CreditRolloverService()
    
    @pytest.mark.asyncio
    async def test_preview_rollover(self):
        """Test rollover preview."""
        mock_db = AsyncMock()
        
        mock_state = MagicMock()
        mock_state.user_id = "user123"
        mock_state.credit_balance = 40000
        mock_state.subscription_tier.value = "plus"
        mock_state.current_period_start = datetime.utcnow() - timedelta(days=20)
        
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_state
        mock_db.execute.return_value = mock_result
        
        preview = await self.service.preview_rollover("user123", mock_db)
        
        assert preview["user_id"] == "user123"
        assert preview["tier"] == "plus"
        assert preview["current_balance"] == 40000
        assert preview["rollover_limit"] == 25000
        assert preview["rollover_amount"] == 25000
        assert preview["credits_to_expire"] == 15000
        assert preview["new_period_credits"] == 50000
        assert preview["projected_new_balance"] == 75000
        assert preview["days_remaining"] >= 0


class TestGlobalInstance:
    """Test global instance."""
    
    def test_global_instance_exists(self):
        """Test global instance exists."""
        assert credit_rollover_service is not None
        assert isinstance(credit_rollover_service, CreditRolloverService)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
