"""
Prometheus metrics for Billing Service.

Exposes metrics for:
- Request counts and latencies
- Subscription operations
- Credit transactions
- Payment processing
- Usage metering
"""
from prometheus_client import Counter, Histogram, Gauge, Info
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from fastapi import APIRouter, Response

router = APIRouter()

# ============================================
# SERVICE INFO
# ============================================
BILLING_SERVICE_INFO = Info('billing_service', 'Billing service information')
BILLING_SERVICE_INFO.info({
    'version': '1.0.0',
    'service': 'billing_service',
})

# ============================================
# REQUEST METRICS
# ============================================
REQUEST_COUNT = Counter(
    'billing_requests_total',
    'Total number of requests to billing service',
    ['method', 'endpoint', 'status']
)

REQUEST_LATENCY = Histogram(
    'billing_request_latency_seconds',
    'Request latency in seconds',
    ['method', 'endpoint'],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]
)

# ============================================
# SUBSCRIPTION METRICS
# ============================================
SUBSCRIPTION_CREATED = Counter(
    'billing_subscription_created_total',
    'Total subscriptions created',
    ['plan', 'billing_cycle']
)

SUBSCRIPTION_CANCELED = Counter(
    'billing_subscription_canceled_total',
    'Total subscriptions canceled',
    ['plan', 'reason']
)

SUBSCRIPTION_CHANGED = Counter(
    'billing_subscription_changed_total',
    'Total subscription plan changes',
    ['from_plan', 'to_plan']
)

ACTIVE_SUBSCRIPTIONS = Gauge(
    'billing_active_subscriptions',
    'Number of active subscriptions',
    ['plan']
)

# ============================================
# CREDIT METRICS
# ============================================
CREDITS_PURCHASED = Counter(
    'billing_credits_purchased_total',
    'Total credits purchased',
    ['plan']
)

CREDITS_PURCHASED_USD = Counter(
    'billing_credits_purchased_usd_total',
    'Total USD spent on credits'
)

CREDITS_CONSUMED = Counter(
    'billing_credits_consumed_total',
    'Total credits consumed',
    ['usage_type']
)

CREDITS_BALANCE = Gauge(
    'billing_credits_balance_total',
    'Total credit balance across all users'
)

# ============================================
# PAYMENT METRICS
# ============================================
PAYMENT_SUCCESS = Counter(
    'billing_payment_success_total',
    'Total successful payments',
    ['type']  # type: subscription/credit_purchase/one_time
)

PAYMENT_FAILED = Counter(
    'billing_payment_failed_total',
    'Total failed payments',
    ['type', 'reason']
)

STRIPE_WEBHOOK_RECEIVED = Counter(
    'billing_stripe_webhook_total',
    'Total Stripe webhooks received',
    ['event_type']
)

STRIPE_WEBHOOK_PROCESSED = Counter(
    'billing_stripe_webhook_processed_total',
    'Total Stripe webhooks successfully processed',
    ['event_type']
)

# ============================================
# REVENUE METRICS
# ============================================
MRR = Gauge(
    'billing_mrr_usd',
    'Monthly Recurring Revenue in USD'
)

TOTAL_REVENUE = Counter(
    'billing_total_revenue_usd',
    'Total revenue in USD'
)

# ============================================
# USAGE METRICS
# ============================================
USAGE_RECORDED = Counter(
    'billing_usage_recorded_total',
    'Total usage records created',
    ['usage_type']
)

USAGE_LIMIT_EXCEEDED = Counter(
    'billing_usage_limit_exceeded_total',
    'Total times usage limits were exceeded',
    ['limit_type']
)

# ============================================
# INVOICE METRICS
# ============================================
INVOICE_CREATED = Counter(
    'billing_invoice_created_total',
    'Total invoices created'
)

INVOICE_PAID = Counter(
    'billing_invoice_paid_total',
    'Total invoices paid'
)

INVOICE_OVERDUE = Gauge(
    'billing_invoice_overdue',
    'Number of overdue invoices'
)

# ============================================
# CHECKOUT METRICS
# ============================================
CHECKOUT_STARTED = Counter(
    'billing_checkout_started_total',
    'Total checkout sessions started',
    ['type']  # type: subscription/credits
)

CHECKOUT_COMPLETED = Counter(
    'billing_checkout_completed_total',
    'Total checkout sessions completed',
    ['type']
)

CHECKOUT_ABANDONED = Counter(
    'billing_checkout_abandoned_total',
    'Total checkout sessions abandoned',
    ['type']
)

# ============================================
# ECONOMIC STATE METRICS
# ============================================
ECONOMIC_STATE_CHECKS = Counter(
    'billing_economic_state_checks_total',
    'Total economic state checks',
    ['check_type']  # check_type: credits/limits/features
)

ECONOMIC_STATE_BLOCKED = Counter(
    'billing_economic_state_blocked_total',
    'Total requests blocked by economic state',
    ['reason']
)

# ============================================
# METRICS ENDPOINT
# ============================================
@router.get("/metrics")
async def metrics():
    """Expose Prometheus metrics."""
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST
    )


# ============================================
# HELPER FUNCTIONS
# ============================================
def track_request(method: str, endpoint: str, status: int, duration: float):
    """Track a request for metrics."""
    REQUEST_COUNT.labels(method=method, endpoint=endpoint, status=str(status)).inc()
    REQUEST_LATENCY.labels(method=method, endpoint=endpoint).observe(duration)


def track_subscription_created(plan: str, billing_cycle: str):
    """Track subscription creation."""
    SUBSCRIPTION_CREATED.labels(plan=plan, billing_cycle=billing_cycle).inc()


def track_subscription_canceled(plan: str, reason: str = "user_request"):
    """Track subscription cancellation."""
    SUBSCRIPTION_CANCELED.labels(plan=plan, reason=reason).inc()


def track_subscription_changed(from_plan: str, to_plan: str):
    """Track subscription plan change."""
    SUBSCRIPTION_CHANGED.labels(from_plan=from_plan, to_plan=to_plan).inc()


def track_credits_purchased(amount: int, usd: float, plan: str = "unknown"):
    """Track credit purchase."""
    CREDITS_PURCHASED.labels(plan=plan).inc(amount)
    CREDITS_PURCHASED_USD.inc(usd)


def track_credits_consumed(amount: int, usage_type: str):
    """Track credit consumption."""
    CREDITS_CONSUMED.labels(usage_type=usage_type).inc(amount)


def track_payment(success: bool, payment_type: str, reason: str = None):
    """Track payment result."""
    if success:
        PAYMENT_SUCCESS.labels(type=payment_type).inc()
    else:
        PAYMENT_FAILED.labels(type=payment_type, reason=reason or "unknown").inc()


def track_stripe_webhook(event_type: str, processed: bool):
    """Track Stripe webhook."""
    STRIPE_WEBHOOK_RECEIVED.labels(event_type=event_type).inc()
    if processed:
        STRIPE_WEBHOOK_PROCESSED.labels(event_type=event_type).inc()


def track_usage_recorded(usage_type: str):
    """Track usage record creation."""
    USAGE_RECORDED.labels(usage_type=usage_type).inc()


def track_usage_limit_exceeded(limit_type: str):
    """Track usage limit exceeded."""
    USAGE_LIMIT_EXCEEDED.labels(limit_type=limit_type).inc()


def track_checkout(checkout_type: str, status: str):
    """Track checkout session."""
    if status == "started":
        CHECKOUT_STARTED.labels(type=checkout_type).inc()
    elif status == "completed":
        CHECKOUT_COMPLETED.labels(type=checkout_type).inc()
    elif status == "abandoned":
        CHECKOUT_ABANDONED.labels(type=checkout_type).inc()


def track_economic_state_check(check_type: str, blocked: bool = False, reason: str = None):
    """Track economic state check."""
    ECONOMIC_STATE_CHECKS.labels(check_type=check_type).inc()
    if blocked:
        ECONOMIC_STATE_BLOCKED.labels(reason=reason or "unknown").inc()


def update_subscription_gauges(plan_counts: dict):
    """Update subscription gauges."""
    for plan, count in plan_counts.items():
        ACTIVE_SUBSCRIPTIONS.labels(plan=plan).set(count)


def update_revenue_gauges(mrr: float, total: float = None):
    """Update revenue gauges."""
    MRR.set(mrr)
    if total is not None:
        TOTAL_REVENUE.inc(total)


def update_credits_balance(total_balance: int):
    """Update total credits balance gauge."""
    CREDITS_BALANCE.set(total_balance)
