"""Billing Service configuration."""

import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str = os.getenv(
        "BILLING_DATABASE_URL",
        os.getenv(
            "DATABASE_URL",
            f"postgresql+asyncpg://{os.getenv('BILLING_DB_USER', os.getenv('AUTH_DB_USER', 'doadmin'))}:"
            f"{os.getenv('BILLING_DB_PASSWORD', os.getenv('AUTH_DB_PASSWORD', ''))}@"
            f"{os.getenv('BILLING_DB_HOST', os.getenv('AUTH_DB_HOST', 'db'))}:"
            f"{os.getenv('BILLING_DB_PORT', os.getenv('AUTH_DB_PORT', '5432'))}/"
            f"{os.getenv('BILLING_DB_NAME', os.getenv('AUTH_DB_NAME', 'defaultdb'))}?ssl=true"
        )
    )
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://redis:6379/3")
    
    # Environment
    ENVIRONMENT: str = os.getenv("BILLING_ENVIRONMENT", "development")
    
    # Frontend URL (for checkout redirects)
    FRONTEND_URL: str = os.getenv("BILLING_FRONTEND_URL", "https://dev-swat.com")
    
    # Stripe configuration
    STRIPE_SECRET_KEY: str = os.getenv("STRIPE_SECRET_KEY", "")
    STRIPE_WEBHOOK_SECRET: str = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    STRIPE_PUBLISHABLE_KEY: str = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
    
    # Product IDs (set after creating products in Stripe)
    # Matches frontend signupLogic.ts: developer (free), plus ($49/mo), enterprise (custom)
    STRIPE_PRODUCT_DEVELOPER: str = os.getenv("STRIPE_PRODUCT_DEVELOPER", "")
    STRIPE_PRODUCT_PLUS: str = os.getenv("STRIPE_PRODUCT_PLUS", "")
    STRIPE_PRODUCT_ENTERPRISE: str = os.getenv("STRIPE_PRODUCT_ENTERPRISE", "")
    
    # Price IDs (set after creating prices in Stripe)
    # Plus: $49/month, $490/year (2 months free)
    STRIPE_PRICE_PLUS_MONTHLY: str = os.getenv("STRIPE_PRICE_PLUS_MONTHLY", "")
    STRIPE_PRICE_PLUS_YEARLY: str = os.getenv("STRIPE_PRICE_PLUS_YEARLY", "")
    # Enterprise: Custom pricing (contact sales)
    STRIPE_PRICE_ENTERPRISE_MONTHLY: str = os.getenv("STRIPE_PRICE_ENTERPRISE_MONTHLY", "")
    
    # Usage metering
    STRIPE_METER_TOKENS: str = os.getenv("STRIPE_METER_TOKENS", "")
    STRIPE_METER_AGENT_RUNS: str = os.getenv("STRIPE_METER_AGENT_RUNS", "")
    STRIPE_METER_API_CALLS: str = os.getenv("STRIPE_METER_API_CALLS", "")
    
    # Credit pricing (loaded from pricing.yaml via pricing_loader)
    CREDITS_PER_DOLLAR: int = 100  # 100 credits = $1
    MIN_CREDIT_PURCHASE: int = 500  # Minimum $5 purchase
    
    # Service URLs
    CRYPTO_SERVICE_URL: str = os.getenv("CRYPTO_SERVICE_URL", "http://crypto_service:8000")
    NOTIFICATION_SERVICE_URL: str = os.getenv("NOTIFICATION_SERVICE_URL", "")
    
    class Config:
        env_file = ".env"
        extra = "ignore"  # Ignore extra environment variables


settings = Settings()
