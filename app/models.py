"""Billing Service database models."""

from datetime import datetime
import uuid

from sqlalchemy import Column, DateTime, Float, Integer, String, Text, Boolean, JSON, ForeignKey, Numeric
from sqlalchemy.dialects.postgresql import UUID, ARRAY
from sqlalchemy.sql import func

from .db import Base


class Subscription(Base):
    """User subscriptions."""
    __tablename__ = "subscriptions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), index=True, nullable=False)
    
    # Stripe references
    stripe_customer_id = Column(String(64), unique=True, index=True, nullable=True)
    stripe_subscription_id = Column(String(64), unique=True, index=True, nullable=True)
    
    # Plan details
    plan = Column(String(32), default="developer")  # developer, plus, enterprise
    billing_cycle = Column(String(16), default="monthly")  # monthly, yearly
    
    # Status
    status = Column(String(32), default="active")  # active, past_due, canceled, trialing, paused
    
    # Dates
    current_period_start = Column(DateTime(timezone=True), nullable=True)
    current_period_end = Column(DateTime(timezone=True), nullable=True)
    trial_start = Column(DateTime(timezone=True), nullable=True)
    trial_end = Column(DateTime(timezone=True), nullable=True)
    canceled_at = Column(DateTime(timezone=True), nullable=True)
    
    # Pricing
    price_id = Column(String(64), nullable=True)
    amount = Column(Numeric(precision=10, scale=2), nullable=True)
    currency = Column(String(3), default="usd")
    
    # Extra metadata
    extra_metadata = Column(JSON, nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class CreditBalance(Base):
    """User credit balances."""
    __tablename__ = "credit_balances"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), unique=True, index=True, nullable=False)
    
    # Balances
    balance = Column(Integer, default=0)  # Current available credits
    lifetime_purchased = Column(Integer, default=0)  # Total credits ever purchased
    lifetime_used = Column(Integer, default=0)  # Total credits ever used
    lifetime_bonus = Column(Integer, default=0)  # Total bonus credits received
    
    # Billing period tracking (30-day cycle)
    period_start = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    period_end = Column(DateTime(timezone=True), nullable=True)  # period_start + 30 days
    period_credits_granted = Column(Integer, default=0)  # Credits granted this period
    
    # Expiring credits
    expiring_credits = Column(Integer, default=0)
    expiration_date = Column(DateTime(timezone=True), nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class CreditTransaction(Base):
    """Credit transaction history."""
    __tablename__ = "credit_transactions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), index=True, nullable=False)
    
    # Transaction type
    tx_type = Column(String(32), nullable=False)  # purchase, usage, bonus, refund, expiration
    
    # Amounts
    amount = Column(Integer, nullable=False)  # Positive for credit, negative for debit
    balance_after = Column(Integer, nullable=False)
    
    # Reference
    reference_type = Column(String(32), nullable=True)  # agent_run, api_call, token_usage, etc.
    reference_id = Column(UUID(as_uuid=True), nullable=True)
    
    # Payment reference
    stripe_payment_intent_id = Column(String(64), nullable=True)
    
    description = Column(Text, nullable=True)
    extra_metadata = Column(JSON, nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class UsageRecord(Base):
    """Usage metering records."""
    __tablename__ = "usage_records"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), index=True, nullable=False)
    subscription_id = Column(UUID(as_uuid=True), index=True, nullable=True)
    
    # Usage type
    usage_type = Column(String(32), nullable=False)  # tokens, agent_runs, api_calls, storage
    
    # Quantity
    quantity = Column(Integer, nullable=False)
    unit = Column(String(16), nullable=True)  # tokens, runs, calls, mb
    
    # Billing period
    period_start = Column(DateTime(timezone=True), nullable=False)
    period_end = Column(DateTime(timezone=True), nullable=False)
    
    # Stripe metering
    stripe_usage_record_id = Column(String(64), nullable=True)
    reported_to_stripe = Column(Boolean, default=False)
    
    # Pricing
    unit_price = Column(Numeric(precision=10, scale=6), nullable=True)
    total_cost = Column(Numeric(precision=10, scale=2), nullable=True)
    
    extra_metadata = Column(JSON, nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Invoice(Base):
    """Invoice records."""
    __tablename__ = "invoices"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), index=True, nullable=False)
    
    # Stripe reference
    stripe_invoice_id = Column(String(64), unique=True, index=True, nullable=True)
    stripe_invoice_pdf = Column(String(512), nullable=True)
    stripe_hosted_invoice_url = Column(String(512), nullable=True)
    
    # Invoice details
    invoice_number = Column(String(32), unique=True, nullable=False)
    status = Column(String(32), default="draft")  # draft, open, paid, void, uncollectible
    
    # Amounts
    subtotal = Column(Numeric(precision=10, scale=2), nullable=False)
    tax = Column(Numeric(precision=10, scale=2), default=0)
    total = Column(Numeric(precision=10, scale=2), nullable=False)
    amount_paid = Column(Numeric(precision=10, scale=2), default=0)
    amount_due = Column(Numeric(precision=10, scale=2), nullable=False)
    currency = Column(String(3), default="usd")
    
    # Line items
    line_items = Column(JSON, nullable=True)
    
    # Dates
    period_start = Column(DateTime(timezone=True), nullable=True)
    period_end = Column(DateTime(timezone=True), nullable=True)
    due_date = Column(DateTime(timezone=True), nullable=True)
    paid_at = Column(DateTime(timezone=True), nullable=True)
    
    # Billing info
    billing_name = Column(String(255), nullable=True)
    billing_email = Column(String(255), nullable=True)
    billing_address = Column(JSON, nullable=True)
    
    extra_metadata = Column(JSON, nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PaymentMethod(Base):
    """Stored payment methods."""
    __tablename__ = "payment_methods"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), index=True, nullable=False)
    
    # Stripe reference
    stripe_payment_method_id = Column(String(64), unique=True, index=True, nullable=False)
    
    # Card details (tokenized)
    card_brand = Column(String(16), nullable=True)
    card_last_four = Column(String(4), nullable=True)
    card_exp_month = Column(Integer, nullable=True)
    card_exp_year = Column(Integer, nullable=True)
    
    # Status
    is_default = Column(Boolean, default=False)
    status = Column(String(32), default="active")  # active, expired, removed
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PricingPlan(Base):
    """Pricing plan definitions."""
    __tablename__ = "pricing_plans"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    # Plan identity
    name = Column(String(32), unique=True, nullable=False)  # developer, plus, enterprise
    display_name = Column(String(64), nullable=False)
    description = Column(Text, nullable=True)
    
    # Pricing
    monthly_price = Column(Numeric(precision=10, scale=2), nullable=True)
    yearly_price = Column(Numeric(precision=10, scale=2), nullable=True)
    currency = Column(String(3), default="usd")
    
    # Stripe references
    stripe_product_id = Column(String(64), nullable=True)
    stripe_price_monthly_id = Column(String(64), nullable=True)
    stripe_price_yearly_id = Column(String(64), nullable=True)
    
    # Features/limits
    features = Column(JSON, nullable=True)
    limits = Column(JSON, nullable=True)  # requests_per_day, tokens_per_day, etc.
    
    # Status
    is_active = Column(Boolean, default=True)
    is_public = Column(Boolean, default=True)
    
    # Ordering
    sort_order = Column(Integer, default=0)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class Coupon(Base):
    """Discount coupons."""
    __tablename__ = "coupons"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    # Coupon identity
    code = Column(String(32), unique=True, index=True, nullable=False)
    stripe_coupon_id = Column(String(64), nullable=True)
    
    # Discount
    discount_type = Column(String(16), nullable=False)  # percent, fixed
    discount_value = Column(Numeric(precision=10, scale=2), nullable=False)
    currency = Column(String(3), default="usd")
    
    # Validity
    valid_from = Column(DateTime(timezone=True), nullable=True)
    valid_until = Column(DateTime(timezone=True), nullable=True)
    max_redemptions = Column(Integer, nullable=True)
    redemption_count = Column(Integer, default=0)
    
    # Restrictions
    applies_to_plans = Column(ARRAY(String), nullable=True)
    min_amount = Column(Numeric(precision=10, scale=2), nullable=True)
    first_time_only = Column(Boolean, default=False)
    
    # Status
    is_active = Column(Boolean, default=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
