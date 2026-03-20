"""
Tests for Webhook Processor Service - Phase 1.2 GTM

Tests Stripe webhook reliability with retry logic.
"""

import pytest
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from app.webhook_processor import (
    WebhookProcessor,
    WebhookEvent,
    WebhookStatus,
    RETRY_DELAYS,
    handle_checkout_completed,
    handle_invoice_paid,
    handle_invoice_payment_failed,
    handle_subscription_updated,
    handle_subscription_deleted,
    handle_payment_intent_succeeded,
    create_webhook_processor,
)


class TestWebhookStatus:
    """Test WebhookStatus enum."""
    
    def test_status_values(self):
        """Test all status values exist."""
        assert WebhookStatus.PENDING.value == "pending"
        assert WebhookStatus.PROCESSING.value == "processing"
        assert WebhookStatus.COMPLETED.value == "completed"
        assert WebhookStatus.FAILED.value == "failed"
        assert WebhookStatus.DEAD_LETTER.value == "dead_letter"


class TestRetryDelays:
    """Test retry delay configuration."""
    
    def test_retry_delays_defined(self):
        """Test retry delays are defined."""
        assert len(RETRY_DELAYS) == 5
        assert RETRY_DELAYS[0] == 60      # 1 minute
        assert RETRY_DELAYS[1] == 300     # 5 minutes
        assert RETRY_DELAYS[2] == 900     # 15 minutes
        assert RETRY_DELAYS[3] == 3600    # 1 hour
        assert RETRY_DELAYS[4] == 86400   # 24 hours
    
    def test_retry_delays_increasing(self):
        """Test retry delays are increasing (exponential backoff)."""
        for i in range(len(RETRY_DELAYS) - 1):
            assert RETRY_DELAYS[i] < RETRY_DELAYS[i + 1]


class TestWebhookProcessor:
    """Test WebhookProcessor class."""
    
    def setup_method(self):
        """Setup test fixtures."""
        self.mock_db = AsyncMock()
        self.processor = WebhookProcessor(self.mock_db)
    
    def test_register_handler(self):
        """Test registering event handlers."""
        async def test_handler(payload):
            return {"handled": True}
        
        self.processor.register_handler("test.event", test_handler)
        
        assert "test.event" in self.processor._handlers
        assert self.processor._handlers["test.event"] == test_handler
    
    @pytest.mark.asyncio
    async def test_process_event_already_completed(self):
        """Test processing already completed event returns cached result."""
        # Mock existing completed event
        existing_event = MagicMock()
        existing_event.status = WebhookStatus.COMPLETED.value
        existing_event.result = {"previous": "result"}
        
        # Mock _get_event to return the existing event
        with patch.object(self.processor, '_get_event', new_callable=AsyncMock) as mock_get:
            mock_get.return_value = existing_event
            
            result = await self.processor.process_event(
                "evt_123",
                "checkout.session.completed",
                {"data": "test"}
            )
        
        assert result["status"] == "already_processed"
        assert result["result"] == {"previous": "result"}
    
    @pytest.mark.asyncio
    async def test_process_event_currently_processing(self):
        """Test processing event that's currently being processed."""
        existing_event = MagicMock()
        existing_event.status = WebhookStatus.PROCESSING.value
        
        with patch.object(self.processor, '_get_event', new_callable=AsyncMock) as mock_get:
            mock_get.return_value = existing_event
            
            result = await self.processor.process_event(
                "evt_456",
                "checkout.session.completed",
                {"data": "test"}
            )
        
        assert result["status"] == "processing"
    
    @pytest.mark.asyncio
    async def test_process_event_no_handler(self):
        """Test processing event with no registered handler."""
        mock_event = MagicMock()
        mock_event.status = WebhookStatus.PENDING.value
        mock_event.attempts = 0
        
        with patch.object(self.processor, '_get_event', new_callable=AsyncMock) as mock_get:
            mock_get.return_value = None
            with patch.object(self.processor, '_create_or_get_event', new_callable=AsyncMock) as mock_create:
                mock_create.return_value = mock_event
                
                result = await self.processor.process_event(
                    "evt_789",
                    "unknown.event.type",
                    {"data": "test"}
                )
        
        assert mock_event.status == WebhookStatus.COMPLETED.value
    
    @pytest.mark.asyncio
    async def test_process_event_success(self):
        """Test successful event processing."""
        mock_event = MagicMock()
        mock_event.status = WebhookStatus.PENDING.value
        mock_event.attempts = 0
        
        async def success_handler(payload):
            return {"success": True}
        
        self.processor.register_handler("test.success", success_handler)
        
        with patch.object(self.processor, '_get_event', new_callable=AsyncMock) as mock_get:
            mock_get.return_value = None
            with patch.object(self.processor, '_create_or_get_event', new_callable=AsyncMock) as mock_create:
                mock_create.return_value = mock_event
                
                result = await self.processor.process_event(
                    "evt_success",
                    "test.success",
                    {"data": "test"}
                )
        
        assert result["status"] == "completed"
        assert mock_event.status == WebhookStatus.COMPLETED.value
    
    @pytest.mark.asyncio
    async def test_process_event_failure_with_retry(self):
        """Test failed event processing schedules retry."""
        mock_event = MagicMock()
        mock_event.status = WebhookStatus.PENDING.value
        mock_event.attempts = 0
        mock_event.max_attempts = 5
        
        async def failing_handler(payload):
            raise Exception("Test failure")
        
        self.processor.register_handler("test.fail", failing_handler)
        
        with patch.object(self.processor, '_get_event', new_callable=AsyncMock) as mock_get:
            mock_get.return_value = None
            with patch.object(self.processor, '_create_or_get_event', new_callable=AsyncMock) as mock_create:
                mock_create.return_value = mock_event
                
                result = await self.processor.process_event(
                    "evt_fail",
                    "test.fail",
                    {"data": "test"}
                )
        
        assert result["status"] == "failed"
        assert result["retry_scheduled"] is True
        assert mock_event.status == WebhookStatus.PENDING.value
    
    @pytest.mark.asyncio
    async def test_process_event_max_retries_dead_letter(self):
        """Test event moves to dead letter after max retries."""
        mock_event = MagicMock()
        mock_event.status = WebhookStatus.PENDING.value
        mock_event.attempts = 4  # Will be 5 after increment
        mock_event.max_attempts = 5
        
        async def failing_handler(payload):
            raise Exception("Test failure")
        
        self.processor.register_handler("test.fail", failing_handler)
        
        with patch.object(self.processor, '_get_event', new_callable=AsyncMock) as mock_get:
            mock_get.return_value = None
            with patch.object(self.processor, '_create_or_get_event', new_callable=AsyncMock) as mock_create:
                mock_create.return_value = mock_event
                
                result = await self.processor.process_event(
                    "evt_dead",
                    "test.fail",
                    {"data": "test"}
                )
        
        assert result["status"] == "failed"
        assert result["retry_scheduled"] is False
        assert mock_event.status == WebhookStatus.DEAD_LETTER.value


class TestWebhookHandlers:
    """Test individual webhook handlers."""
    
    @pytest.mark.asyncio
    async def test_handle_checkout_completed(self):
        """Test checkout.session.completed handler."""
        payload = {
            "data": {
                "object": {
                    "customer": "cus_123",
                    "subscription": "sub_456",
                    "metadata": {
                        "user_id": "user_789",
                        "tier": "plus",
                    }
                }
            }
        }
        
        result = await handle_checkout_completed(payload)
        
        assert result["action"] == "subscription_created"
        assert result["user_id"] == "user_789"
        assert result["tier"] == "plus"
        assert result["subscription_id"] == "sub_456"
    
    @pytest.mark.asyncio
    async def test_handle_checkout_completed_missing_user_id(self):
        """Test checkout handler with missing user_id."""
        payload = {
            "data": {
                "object": {
                    "customer": "cus_123",
                    "metadata": {}
                }
            }
        }
        
        result = await handle_checkout_completed(payload)
        
        assert result["error"] == "missing_user_id"
    
    @pytest.mark.asyncio
    async def test_handle_invoice_paid(self):
        """Test invoice.paid handler."""
        payload = {
            "data": {
                "object": {
                    "customer": "cus_123",
                    "subscription": "sub_456",
                    "amount_paid": 4900,
                }
            }
        }
        
        result = await handle_invoice_paid(payload)
        
        assert result["action"] == "invoice_paid"
        assert result["customer_id"] == "cus_123"
        assert result["amount_paid"] == 4900
    
    @pytest.mark.asyncio
    async def test_handle_invoice_payment_failed(self):
        """Test invoice.payment_failed handler."""
        payload = {
            "data": {
                "object": {
                    "customer": "cus_123",
                    "subscription": "sub_456",
                }
            }
        }
        
        result = await handle_invoice_payment_failed(payload)
        
        assert result["action"] == "payment_failed"
        assert result["customer_id"] == "cus_123"
    
    @pytest.mark.asyncio
    async def test_handle_subscription_updated(self):
        """Test customer.subscription.updated handler."""
        payload = {
            "data": {
                "object": {
                    "id": "sub_123",
                    "status": "active",
                }
            }
        }
        
        result = await handle_subscription_updated(payload)
        
        assert result["action"] == "subscription_updated"
        assert result["subscription_id"] == "sub_123"
        assert result["status"] == "active"
    
    @pytest.mark.asyncio
    async def test_handle_subscription_deleted(self):
        """Test customer.subscription.deleted handler."""
        payload = {
            "data": {
                "object": {
                    "id": "sub_123",
                    "customer": "cus_456",
                }
            }
        }
        
        result = await handle_subscription_deleted(payload)
        
        assert result["action"] == "subscription_deleted"
        assert result["subscription_id"] == "sub_123"
    
    @pytest.mark.asyncio
    async def test_handle_payment_intent_succeeded_credits(self):
        """Test payment_intent.succeeded handler for credit purchase."""
        payload = {
            "data": {
                "object": {
                    "id": "pi_123",
                    "amount": 800,
                    "metadata": {
                        "user_id": "user_456",
                        "credits": "10000",
                        "pack_type": "starter",
                    }
                }
            }
        }
        
        result = await handle_payment_intent_succeeded(payload)
        
        assert result["action"] == "credits_purchased"
        assert result["user_id"] == "user_456"
        assert result["credits"] == 10000
        assert result["pack_type"] == "starter"
    
    @pytest.mark.asyncio
    async def test_handle_payment_intent_succeeded_no_credits(self):
        """Test payment_intent.succeeded handler without credit metadata."""
        payload = {
            "data": {
                "object": {
                    "id": "pi_123",
                    "amount": 4900,
                    "metadata": {}
                }
            }
        }
        
        result = await handle_payment_intent_succeeded(payload)
        
        assert result["action"] == "payment_succeeded"
        assert result["amount"] == 4900


class TestCreateWebhookProcessor:
    """Test webhook processor factory function."""
    
    def test_create_webhook_processor(self):
        """Test creating processor with all handlers registered."""
        mock_db = AsyncMock()
        processor = create_webhook_processor(mock_db)
        
        assert isinstance(processor, WebhookProcessor)
        
        # Check all handlers are registered
        expected_handlers = [
            "checkout.session.completed",
            "invoice.paid",
            "invoice.payment_failed",
            "customer.subscription.updated",
            "customer.subscription.deleted",
            "payment_intent.succeeded",
        ]
        
        for handler_name in expected_handlers:
            assert handler_name in processor._handlers


class TestWebhookEventModel:
    """Test WebhookEvent SQLAlchemy model."""
    
    def test_webhook_event_defaults(self):
        """Test WebhookEvent default values."""
        # This tests the model definition, not actual DB operations
        assert WebhookEvent.__tablename__ == "webhook_events"
        
        # Check columns exist
        columns = [c.name for c in WebhookEvent.__table__.columns]
        assert "id" in columns
        assert "stripe_event_id" in columns
        assert "event_type" in columns
        assert "payload" in columns
        assert "status" in columns
        assert "attempts" in columns
        assert "max_attempts" in columns
        assert "error_message" in columns
        assert "result" in columns


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
