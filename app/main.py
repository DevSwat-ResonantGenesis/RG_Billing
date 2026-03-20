import sys
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routers import router as billing_router, dashboard_router
from .economic_state_api import router as economic_state_router
from .cache import init_cache, shutdown_cache, get_cache
from .cron_jobs import start_scheduler, stop_scheduler
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Deterministic sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    # Startup
    logger.info("Starting Billing Service...")
    await init_cache()
    logger.info("Cache initialized")
    
    # Start cron scheduler for credit expiration and rollover
    start_scheduler()
    logger.info("Cron scheduler started")
    
    yield
    
    # Shutdown
    logger.info("Shutting down Billing Service...")
    stop_scheduler()
    logger.info("Cron scheduler stopped")
    await shutdown_cache()


# Single service entrypoint
app = FastAPI(
    title="Billing_Service Service",
    description="Service for Genesis2026",
    version="1.0.0",
    lifespan=lifespan,
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Health check endpoint
@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "billing_service"}


# Cache health endpoint for monitoring
@app.get("/cache/health")
async def cache_health():
    """Get cache health and statistics for monitoring."""
    cache = get_cache()
    return await cache.health_check()

# Root endpoint
@app.get("/")
async def root():
    return {"message": f"Billing_Service Service is running"}

# Service-specific endpoint
@app.get("/api/v1/status")
async def status():
    return {"service": "billing_service", "status": "active", "version": "1.0.0"}

# Billing API routes (support both legacy and /api-prefixed paths)
app.include_router(billing_router)
app.include_router(billing_router, prefix="/api")
# Economic State routes (gateway contract)
app.include_router(economic_state_router)
# Dashboard routes (for gateway compatibility - gateway proxies to /dashboard/me)
app.include_router(dashboard_router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8003)
