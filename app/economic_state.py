"""
UserEconomicState - The canonical economic authority for the platform.

This is the SINGLE SOURCE OF TRUTH for:
- Subscription tier and status
- Credit balance
- Hard limits (absolute caps)
- Soft limits (enforced via credits)
- Feature access (capabilities)
- Enforcement mode

NO OTHER SERVICE is allowed to define plans, limits, or features.
Gateway reads this. All execution services read this.
Only billing_service writes this.
"""

from datetime import datetime
from enum import Enum as PyEnum
import uuid

from sqlalchemy import Column, DateTime, Float, Integer, String, Boolean, Enum, CheckConstraint
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func

from .db import Base


# ============================================
# ENUMS (Single Source of Truth)
# ============================================

class SubscriptionTier(str, PyEnum):
    """The ONLY valid subscription tiers. Matches frontend signupLogic.ts PLANS."""
    DEVELOPER = "developer"    # Free tier ($0)
    PLUS = "plus"              # $499/month
    ENTERPRISE = "enterprise"  # Custom pricing
    
    # API Subscriptions
    STATE_PHYSICS_DEV = "state_physics_dev"      # State Physics API - Dev tier
    STATE_PHYSICS_STARTUP = "state_physics_startup"  # State Physics API - Startup tier
    HASH_SPHERE_DEV = "hash_sphere_memory_dev"   # Hash Sphere Memory API - Dev tier
    HASH_SPHERE_STARTUP = "hash_sphere_memory_startup"  # Hash Sphere Memory API - Startup tier


class SubscriptionStatus(str, PyEnum):
    """Subscription lifecycle states."""
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    SUSPENDED = "suspended"


class SubscriptionSource(str, PyEnum):
    """Where the subscription originated."""
    INTERNAL = "internal"
    STRIPE = "stripe"


class EnforcementMode(str, PyEnum):
    """How strictly to enforce limits."""
    STRICT = "strict"   # Hard reject on limit breach
    WARN = "warn"       # Allow but log warning
    OFF = "off"         # No enforcement (dev mode only)


# ============================================
# DEFAULT LIMITS BY TIER
# ============================================

# ============================================
# PRICING (matches frontend signupLogic.ts)
# ============================================

TIER_PRICING = {
    SubscriptionTier.DEVELOPER: {
        "monthly_price": 0,
        "yearly_price": 0,
        "stripe_price_id_monthly": None,
        "stripe_price_id_yearly": None,
    },
    SubscriptionTier.PLUS: {
        "monthly_price": 499,
        "yearly_price": 4990,
        "stripe_price_id_monthly": "price_plus_monthly",
        "stripe_price_id_yearly": "price_plus_yearly",
    },
    SubscriptionTier.ENTERPRISE: {
        "monthly_price": 0,  # Custom
        "yearly_price": 0,   # Custom
        "stripe_price_id_monthly": None,
        "stripe_price_id_yearly": None,
        "contact_sales": True,
    },
}

# ============================================
# CREDIT RATES (1 credit ≈ $0.001)
# ============================================

CREDIT_RATES = {
    "topup_plus": 8.00,      # $8 per 10,000 credits for Plus
    "topup_enterprise": 5.00, # $5 per 10,000 credits for Enterprise
}

TIER_DEFAULTS = {
    # Developer - Free Forever (10K credits/month)
    # ONLY credits control usage - NO quantity limits
    SubscriptionTier.DEVELOPER: {
        "credit_balance": 1_000,
        "credit_rate": 1.0,
        "credit_rollover_max": 0,          # No rollover
        "credit_topup_enabled": False,     # No top-ups
    },
    
    # Plus - $499/month (499K credits/month)
    # ONLY credits control usage - NO quantity limits
    SubscriptionTier.PLUS: {
        "credit_balance": 499_000,         # 499,000 credits/month
        "credit_rate": 1.0,
        "credit_rollover_max": 249_500,    # Rollover up to 249.5K
        "credit_topup_enabled": True,      # Top-ups enabled
        "credit_topup_rate": 8.00,         # $8 per 10,000 credits
    },
    
    # Enterprise - Custom Pricing (unlimited)
    # ONLY credits control usage - NO quantity limits
    SubscriptionTier.ENTERPRISE: {
        "credit_balance": -1,              # Unlimited
        "credit_rate": 0.0,                # No credit cost (custom billing)
        "credit_rollover_max": -1,         # Unlimited rollover
        "credit_topup_enabled": True,
        "credit_topup_rate": 5.00,         # $5 per 10,000 credits (volume discount)
        "custom_runtimes": True,
            "api_access": True,
            "blockchain_access": True,
            "hash_sphere_access": True,
            "kill_switch": "sla_backed",
            "invariants": -1,              # Custom invariants
            "snapshots": -1,               # Unlimited
            "ai_assistance": "full_custom",
            "preview_unlimited": True,
            "sso_saml": True,
            "on_premise": True,
            "hybrid_deployment": True,
            "soc2_hipaa_gdpr": True,
            "sla_guarantee": "99.9%",
        "support": "dedicated_engineers",
        "contact_sales": True,
    },
}


# ============================================
# CANONICAL MODEL
# ============================================

class UserEconomicState(Base):
    """
    The SINGLE authoritative record of a user's economic state.
    
    Invariants:
    - Every user MUST have exactly one UserEconomicState
    - Created atomically during registration
    - Only billing_service can write to this table
    - Gateway and all services READ from this via API
    """
    __tablename__ = "user_economic_states"

    # Primary key
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Identity (from auth_service, immutable after creation)
    user_id = Column(UUID(as_uuid=True), unique=True, index=True, nullable=False)
    org_id = Column(UUID(as_uuid=True), index=True, nullable=False)

    # Subscription
    subscription_tier = Column(
        Enum(SubscriptionTier, name="subscription_tier_enum", create_type=False, values_callable=lambda x: [e.value for e in x]),
        default=SubscriptionTier.DEVELOPER,
        nullable=False
    )
    subscription_status = Column(
        Enum(SubscriptionStatus, name="subscription_status_enum", create_type=False, values_callable=lambda x: [e.value for e in x]),
        default=SubscriptionStatus.ACTIVE,
        nullable=False
    )
    subscription_source = Column(
        Enum(SubscriptionSource, name="subscription_source_enum", create_type=False, values_callable=lambda x: [e.value for e in x]),
        default=SubscriptionSource.INTERNAL,
        nullable=False
    )
    subscription_id = Column(String(64), nullable=True)  # Stripe or internal ID

    # Credits (single currency)
    credit_balance = Column(Integer, default=1_000, nullable=False)
    credit_rate = Column(Float, default=1.0, nullable=False)  # Cost multiplier

    # Hard limits (absolute caps, -1 = unlimited)
    hard_limits = Column(JSONB, nullable=False, default=lambda: TIER_DEFAULTS[SubscriptionTier.DEVELOPER]["hard_limits"])

    # Soft limits (enforced via credits)
    soft_limits = Column(JSONB, nullable=False, default=lambda: TIER_DEFAULTS[SubscriptionTier.DEVELOPER]["soft_limits"])

    # Feature access (capabilities)
    features = Column(JSONB, nullable=False, default=lambda: TIER_DEFAULTS[SubscriptionTier.DEVELOPER]["features"])

    # Enforcement
    enforcement_mode = Column(
        Enum(EnforcementMode, name="enforcement_mode_enum", create_type=False, values_callable=lambda x: [e.value for e in x]),
        default=EnforcementMode.STRICT,
        nullable=False
    )
    is_dev_override = Column(Boolean, default=False, nullable=False)

    # Audit
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Constraints
    __table_args__ = (
        CheckConstraint("credit_balance >= 0", name="credit_balance_non_negative"),
        CheckConstraint("credit_rate >= 0", name="credit_rate_non_negative"),
    )

    def to_gateway_headers(self) -> dict:
        """
        Generate headers for gateway to inject into downstream requests.
        These are the ONLY headers services should trust for economic state.
        """
        return {
            "X-User-Id": str(self.user_id),
            "X-Org-Id": str(self.org_id),
            "X-Subscription-Tier": self.subscription_tier.value,
            "X-Subscription-Status": self.subscription_status.value,
            "X-Credit-Balance": str(self.credit_balance),
            "X-Credit-Rate": str(self.credit_rate),
            "X-Enforcement-Mode": self.enforcement_mode.value,
            "X-Is-Dev-Override": str(self.is_dev_override).lower(),
        }

    def to_dict(self) -> dict:
        """Full serialization for API responses."""
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "org_id": str(self.org_id),
            "subscription_tier": self.subscription_tier.value,
            "subscription_status": self.subscription_status.value,
            "subscription_source": self.subscription_source.value,
            "subscription_id": self.subscription_id,
            "credit_balance": self.credit_balance,
            "credit_rate": self.credit_rate,
            "hard_limits": self.hard_limits,
            "soft_limits": self.soft_limits,
            "features": self.features,
            "enforcement_mode": self.enforcement_mode.value,
            "is_dev_override": self.is_dev_override,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    @classmethod
    def create_for_tier(
        cls,
        user_id: uuid.UUID,
        org_id: uuid.UUID,
        tier: SubscriptionTier = SubscriptionTier.DEVELOPER,
        subscription_source: SubscriptionSource = SubscriptionSource.INTERNAL,
        subscription_id: str = None,
        is_dev_override: bool = False,
    ) -> "UserEconomicState":
        """
        Factory method to create a UserEconomicState with tier defaults.
        This is the ONLY way to create a new economic state.
        """
        defaults = TIER_DEFAULTS[tier]
        
        return cls(
            user_id=user_id,
            org_id=org_id,
            subscription_tier=tier,
            subscription_status=SubscriptionStatus.ACTIVE,
            subscription_source=subscription_source,
            subscription_id=subscription_id,
            credit_balance=defaults["credit_balance"],
            credit_rate=defaults["credit_rate"],
            hard_limits=defaults["hard_limits"],
            soft_limits=defaults["soft_limits"],
            features=defaults["features"],
            enforcement_mode=EnforcementMode.OFF if is_dev_override else EnforcementMode.STRICT,
            is_dev_override=is_dev_override,
        )

    def upgrade_to_tier(self, new_tier: SubscriptionTier) -> None:
        """
        Upgrade subscription tier and apply new limits.
        Credits are NOT reset - they accumulate.
        """
        defaults = TIER_DEFAULTS[new_tier]
        
        self.subscription_tier = new_tier
        self.credit_rate = defaults["credit_rate"]
        self.hard_limits = defaults["hard_limits"]
        self.soft_limits = defaults["soft_limits"]
        self.features = defaults["features"]
        
        # Add tier bonus credits (not replace)
        tier_bonus = defaults["credit_balance"] - TIER_DEFAULTS[self.subscription_tier]["credit_balance"]
        if tier_bonus > 0:
            self.credit_balance += tier_bonus

    def can_access_feature(self, feature_name: str) -> bool:
        """Check if user can access a specific feature."""
        if self.is_dev_override:
            return True
        return self.features.get(feature_name, False)

    def check_hard_limit(self, limit_name: str, current_value: int) -> tuple[bool, str]:
        """
        Check if a hard limit would be exceeded.
        Returns (allowed, reason).
        """
        if self.is_dev_override:
            return True, ""
        
        limit = self.hard_limits.get(limit_name, 0)
        if limit == -1:  # Unlimited
            return True, ""
        
        if current_value >= limit:
            return False, f"{limit_name} limit reached ({current_value}/{limit})"
        
        return True, ""

    def deduct_credits(self, amount: int) -> tuple[bool, str]:
        """
        Attempt to deduct credits.
        Returns (success, reason).
        """
        if self.is_dev_override or self.credit_rate == 0.0:
            return True, ""
        
        effective_cost = int(amount * self.credit_rate)
        
        if self.enforcement_mode == EnforcementMode.OFF:
            self.credit_balance -= effective_cost
            return True, ""
        
        if self.credit_balance < effective_cost:
            if self.enforcement_mode == EnforcementMode.STRICT:
                return False, f"Insufficient credits ({self.credit_balance} < {effective_cost})"
            # WARN mode - allow but log
            self.credit_balance -= effective_cost
            return True, f"Warning: credits went negative ({self.credit_balance})"
        
        self.credit_balance -= effective_cost
        return True, ""
