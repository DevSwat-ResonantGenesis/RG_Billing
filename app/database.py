"""Database connection for Billing Service - compatibility layer."""

from .db import engine, async_session, Base, get_session

# Alias for compatibility with cron_jobs.py
async_session_maker = async_session
get_db = get_session

__all__ = ["engine", "async_session", "async_session_maker", "Base", "get_session", "get_db"]
