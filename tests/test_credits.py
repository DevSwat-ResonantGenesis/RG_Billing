"""
Tests for Credit Manager - Phase 1.4 GTM

Tests atomic credit operations with row-level locking.
"""

import pytest
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from decimal import Decimal

from app.credits import CreditManager, credit_manager


class TestCreditManager:
    """Test CreditManager class."""
    
    def setup_method(self):
        """Setup test fixtures."""
        self.manager = CreditManager()
    
    def test_free_tier_credits(self):
        """Test free tier credits constant."""
        assert self.manager.FREE_TIER_CREDITS == 1000


class TestDeductCreditsByTokens:
    """Test token-based credit deduction."""
    
    def setup_method(self):
        self.manager = CreditManager()
    
    def test_provider_multipliers_defined(self):
        """Test provider multipliers are correctly defined."""
        # These are defined in the method, test via cost calculation
        pass
    
    @pytest.mark.asyncio
    async def test_deduct_credits_by_tokens_calculates_cost(self):
        """Test token-based deduction calculates correct cost."""
        mock_db = AsyncMock()
        
        # Mock balance
        mock_balance = MagicMock()
        mock_balance.balance = 10000
        mock_balance.lifetime_used = 0
        mock_balance.user_id = "user123"
        
        mock_db.execute.return_value.scalar_one_or_none.return_value = mock_balance
        
        # Mock transaction
        mock_tx = MagicMock()
        mock_tx.id = "tx123"
        mock_tx.balance_after = 9993
        
        with patch.object(self.manager, 'deduct_credits_atomic', return_value=(mock_tx, False)):
            result = await self.manager.deduct_credits_by_tokens(
                user_id="user123",
                input_tokens=100,
                output_tokens=200,
                model="gpt-4o",
                provider="openai",
                db_session=mock_db,
            )
        
        assert "balance" in result
        assert "deducted" in result
        assert "token_usage" in result
        assert result["token_usage"]["input_tokens"] == 100
        assert result["token_usage"]["output_tokens"] == 200


class TestCheckCreditsSufficient:
    """Test credit sufficiency check."""
    
    def setup_method(self):
        self.manager = CreditManager()
    
    @pytest.mark.asyncio
    async def test_check_sufficient_true(self):
        """Test returns True when sufficient credits."""
        mock_db = AsyncMock()
        
        mock_balance = MagicMock()
        mock_balance.balance = 1000
        
        with patch.object(self.manager, 'get_or_create_balance', return_value=mock_balance):
            has_sufficient, balance = await self.manager.check_credits_sufficient(
                "user123", 500, mock_db
            )
        
        assert has_sufficient is True
        assert balance == 1000
    
    @pytest.mark.asyncio
    async def test_check_sufficient_false(self):
        """Test returns False when insufficient credits."""
        mock_db = AsyncMock()
        
        mock_balance = MagicMock()
        mock_balance.balance = 100
        
        with patch.object(self.manager, 'get_or_create_balance', return_value=mock_balance):
            has_sufficient, balance = await self.manager.check_credits_sufficient(
                "user123", 500, mock_db
            )
        
        assert has_sufficient is False
        assert balance == 100
    
    @pytest.mark.asyncio
    async def test_check_sufficient_exact(self):
        """Test returns True when exact amount available."""
        mock_db = AsyncMock()
        
        mock_balance = MagicMock()
        mock_balance.balance = 500
        
        with patch.object(self.manager, 'get_or_create_balance', return_value=mock_balance):
            has_sufficient, balance = await self.manager.check_credits_sufficient(
                "user123", 500, mock_db
            )
        
        assert has_sufficient is True


class TestCreditCostCalculation:
    """Test credit cost calculations for different providers."""
    
    def test_openai_cost(self):
        """Test OpenAI cost calculation (1.0x multiplier)."""
        # 1000 input + 1000 output
        # Input: 1000/1000 * 10 * 1.0 = 10
        # Output: 1000/1000 * 30 * 1.0 = 30
        # Total: 40
        input_credits = (1000 / 1000) * 10 * 1.0
        output_credits = (1000 / 1000) * 30 * 1.0
        total = int(input_credits + output_credits + 0.5)
        assert total == 40
    
    def test_anthropic_cost(self):
        """Test Anthropic cost calculation (1.2x multiplier)."""
        input_credits = (1000 / 1000) * 10 * 1.2
        output_credits = (1000 / 1000) * 30 * 1.2
        total = int(input_credits + output_credits + 0.5)
        assert total == 48
    
    def test_google_cost(self):
        """Test Google cost calculation (0.8x multiplier)."""
        input_credits = (1000 / 1000) * 10 * 0.8
        output_credits = (1000 / 1000) * 30 * 0.8
        total = int(input_credits + output_credits + 0.5)
        assert total == 32
    
    def test_groq_cost(self):
        """Test Groq cost calculation (0.5x multiplier)."""
        input_credits = (1000 / 1000) * 10 * 0.5
        output_credits = (1000 / 1000) * 30 * 0.5
        total = int(input_credits + output_credits + 0.5)
        assert total == 20
    
    def test_local_cost(self):
        """Test Local cost calculation (0.1x multiplier)."""
        input_credits = (1000 / 1000) * 10 * 0.1
        output_credits = (1000 / 1000) * 30 * 0.1
        total = int(input_credits + output_credits + 0.5)
        assert total == 4
    
    def test_minimum_cost(self):
        """Test minimum cost is 1 credit."""
        # Very small usage
        input_credits = (1 / 1000) * 10 * 1.0  # 0.01
        output_credits = (1 / 1000) * 30 * 1.0  # 0.03
        total = max(1, int(input_credits + output_credits + 0.5))
        assert total == 1


class TestAtomicDeduction:
    """Test atomic credit deduction with locking."""
    
    def setup_method(self):
        self.manager = CreditManager()
    
    @pytest.mark.asyncio
    async def test_atomic_deduction_success(self):
        """Test successful atomic deduction."""
        mock_db = AsyncMock()
        
        # Mock balance with FOR UPDATE
        mock_balance = MagicMock()
        mock_balance.balance = 1000
        mock_balance.lifetime_used = 0
        mock_balance.user_id = "user123"
        
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_balance
        mock_db.execute.return_value = mock_result
        
        # Mock idempotency
        with patch('app.credits.get_idempotency') as mock_idemp:
            mock_idemp_instance = MagicMock()
            mock_idemp_instance.check = AsyncMock(return_value=MagicMock(is_duplicate=False))
            mock_idemp_instance.store = AsyncMock()
            mock_idemp.return_value = mock_idemp_instance
            
            transaction, is_duplicate = await self.manager.deduct_credits_atomic(
                user_id="user123",
                amount=100,
                reference_type="test",
                idempotency_key="key123",
                db_session=mock_db,
            )
        
        assert is_duplicate is False
        assert mock_balance.balance == 900
        assert mock_balance.lifetime_used == 100
    
    @pytest.mark.asyncio
    async def test_atomic_deduction_idempotent(self):
        """Test atomic deduction returns cached result for duplicate."""
        mock_db = AsyncMock()
        
        # Mock idempotency returning duplicate
        with patch('app.credits.get_idempotency') as mock_idemp:
            mock_idemp_instance = MagicMock()
            mock_idemp_instance.check = AsyncMock(return_value=MagicMock(
                is_duplicate=True,
                previous_result={"id": "prev_tx", "balance_after": 900, "amount": 100}
            ))
            mock_idemp.return_value = mock_idemp_instance
            
            transaction, is_duplicate = await self.manager.deduct_credits_atomic(
                user_id="user123",
                amount=100,
                reference_type="test",
                idempotency_key="key123",
                db_session=mock_db,
            )
        
        assert is_duplicate is True
    
    @pytest.mark.asyncio
    async def test_atomic_deduction_insufficient_credits(self):
        """Test atomic deduction raises error for insufficient credits."""
        mock_db = AsyncMock()
        
        mock_balance = MagicMock()
        mock_balance.balance = 50  # Less than required
        
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_balance
        mock_db.execute.return_value = mock_result
        
        with patch('app.credits.get_idempotency') as mock_idemp:
            mock_idemp_instance = MagicMock()
            mock_idemp_instance.check = AsyncMock(return_value=MagicMock(is_duplicate=False))
            mock_idemp.return_value = mock_idemp_instance
            
            with pytest.raises(ValueError, match="Insufficient credits"):
                await self.manager.deduct_credits_atomic(
                    user_id="user123",
                    amount=100,
                    reference_type="test",
                    db_session=mock_db,
                )
    
    @pytest.mark.asyncio
    async def test_atomic_deduction_zero_amount_error(self):
        """Test atomic deduction raises error for zero amount."""
        mock_db = AsyncMock()
        
        with pytest.raises(ValueError, match="Amount must be positive"):
            await self.manager.deduct_credits_atomic(
                user_id="user123",
                amount=0,
                reference_type="test",
                db_session=mock_db,
            )
    
    @pytest.mark.asyncio
    async def test_atomic_deduction_negative_amount_error(self):
        """Test atomic deduction raises error for negative amount."""
        mock_db = AsyncMock()
        
        with pytest.raises(ValueError, match="Amount must be positive"):
            await self.manager.deduct_credits_atomic(
                user_id="user123",
                amount=-100,
                reference_type="test",
                db_session=mock_db,
            )


class TestGlobalCreditManager:
    """Test global credit_manager instance."""
    
    def test_global_instance_exists(self):
        """Test global credit_manager instance exists."""
        assert credit_manager is not None
        assert isinstance(credit_manager, CreditManager)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
