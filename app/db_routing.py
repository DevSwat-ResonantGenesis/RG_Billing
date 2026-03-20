"""
Database Read/Write Routing for Million-User Scale

Implements read replica routing to distribute database load:
- Write operations → Primary database
- Read operations → Read replicas (round-robin)

This dramatically increases read throughput for dashboard queries.
"""

import logging
import random
from typing import Optional, List, AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, AsyncEngine
from sqlalchemy.orm import sessionmaker

from .config import settings

logger = logging.getLogger(__name__)


@dataclass
class DatabaseConfig:
    """Database connection configuration."""
    url: str
    pool_size: int = 20
    max_overflow: int = 30
    pool_timeout: int = 30
    pool_recycle: int = 1800  # 30 minutes
    pool_pre_ping: bool = True
    echo: bool = False


class DatabaseRouter:
    """
    Routes database queries to primary or read replicas.
    
    For million-user scale:
    - All writes go to primary
    - Reads are distributed across replicas
    - Automatic failover to primary if replicas unavailable
    """
    
    def __init__(self):
        self._primary_engine: Optional[AsyncEngine] = None
        self._replica_engines: List[AsyncEngine] = []
        self._primary_session_factory = None
        self._replica_session_factories: List = []
        self._current_replica_index = 0
        self._initialized = False
    
    def _create_engine(self, config: DatabaseConfig) -> AsyncEngine:
        """Create an async engine with production settings."""
        return create_async_engine(
            config.url,
            pool_size=config.pool_size,
            max_overflow=config.max_overflow,
            pool_timeout=config.pool_timeout,
            pool_recycle=config.pool_recycle,
            pool_pre_ping=config.pool_pre_ping,
            echo=config.echo,
        )
    
    async def initialize(
        self,
        primary_url: str,
        replica_urls: Optional[List[str]] = None,
        pool_size: int = 20,
    ):
        """
        Initialize database connections.
        
        Args:
            primary_url: Primary database URL (for writes)
            replica_urls: List of read replica URLs
            pool_size: Connection pool size per database
        """
        if self._initialized:
            return
        
        # Primary database (writes + fallback reads)
        primary_config = DatabaseConfig(
            url=primary_url,
            pool_size=pool_size,
            max_overflow=pool_size,
        )
        self._primary_engine = self._create_engine(primary_config)
        self._primary_session_factory = sessionmaker(
            self._primary_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        
        logger.info(f"Primary database connected: {primary_url[:50]}...")
        
        # Read replicas
        if replica_urls:
            for url in replica_urls:
                replica_config = DatabaseConfig(
                    url=url,
                    pool_size=pool_size,
                    max_overflow=pool_size,
                )
                engine = self._create_engine(replica_config)
                self._replica_engines.append(engine)
                self._replica_session_factories.append(
                    sessionmaker(
                        engine,
                        class_=AsyncSession,
                        expire_on_commit=False,
                    )
                )
            logger.info(f"Connected to {len(replica_urls)} read replicas")
        else:
            logger.info("No read replicas configured - using primary for all queries")
        
        self._initialized = True
    
    async def shutdown(self):
        """Close all database connections."""
        if self._primary_engine:
            await self._primary_engine.dispose()
        
        for engine in self._replica_engines:
            await engine.dispose()
        
        self._initialized = False
        logger.info("Database connections closed")
    
    def _get_replica_session_factory(self):
        """Get next replica session factory (round-robin)."""
        if not self._replica_session_factories:
            return self._primary_session_factory
        
        # Round-robin selection
        factory = self._replica_session_factories[self._current_replica_index]
        self._current_replica_index = (
            self._current_replica_index + 1
        ) % len(self._replica_session_factories)
        
        return factory
    
    @asynccontextmanager
    async def get_write_session(self) -> AsyncGenerator[AsyncSession, None]:
        """
        Get a session for write operations.
        
        Always uses the primary database.
        """
        if not self._initialized:
            raise RuntimeError("Database router not initialized")
        
        async with self._primary_session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
    
    @asynccontextmanager
    async def get_read_session(self) -> AsyncGenerator[AsyncSession, None]:
        """
        Get a session for read operations.
        
        Uses read replicas if available, falls back to primary.
        """
        if not self._initialized:
            raise RuntimeError("Database router not initialized")
        
        factory = self._get_replica_session_factory()
        
        try:
            async with factory() as session:
                yield session
        except Exception as e:
            # Fallback to primary on replica failure
            if factory != self._primary_session_factory:
                logger.warning(f"Replica failed, falling back to primary: {e}")
                async with self._primary_session_factory() as session:
                    yield session
            else:
                raise
    
    @asynccontextmanager
    async def get_session(self, read_only: bool = False) -> AsyncGenerator[AsyncSession, None]:
        """
        Get a database session.
        
        Args:
            read_only: If True, use read replica
        """
        if read_only:
            async with self.get_read_session() as session:
                yield session
        else:
            async with self.get_write_session() as session:
                yield session
    
    def get_stats(self) -> dict:
        """Get connection pool statistics."""
        stats = {
            "initialized": self._initialized,
            "primary_connected": self._primary_engine is not None,
            "replica_count": len(self._replica_engines),
        }
        
        if self._primary_engine:
            pool = self._primary_engine.pool
            stats["primary_pool"] = {
                "size": pool.size(),
                "checked_in": pool.checkedin(),
                "checked_out": pool.checkedout(),
                "overflow": pool.overflow(),
            }
        
        return stats


# Global router instance
db_router = DatabaseRouter()


async def init_db_routing():
    """
    Initialize database routing with read replicas.
    
    Configure via environment variables:
    - DATABASE_URL: Primary database
    - DATABASE_REPLICA_URLS: Comma-separated replica URLs
    """
    import os
    
    primary_url = settings.DATABASE_URL
    
    # Parse replica URLs from environment
    replica_urls_str = os.getenv("DATABASE_REPLICA_URLS", "")
    replica_urls = [url.strip() for url in replica_urls_str.split(",") if url.strip()]
    
    # Pool size based on expected load
    pool_size = int(os.getenv("DB_POOL_SIZE", "20"))
    
    await db_router.initialize(
        primary_url=primary_url,
        replica_urls=replica_urls if replica_urls else None,
        pool_size=pool_size,
    )


async def shutdown_db_routing():
    """Shutdown database routing."""
    await db_router.shutdown()


# Dependency injection helpers for FastAPI
async def get_read_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency for read-only database session."""
    async with db_router.get_read_session() as session:
        yield session


async def get_write_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency for write database session."""
    async with db_router.get_write_session() as session:
        yield session
