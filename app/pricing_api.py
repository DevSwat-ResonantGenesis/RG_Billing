"""
Pricing API - Serve pricing configuration to frontend

This API reads from pricing.yaml and serves it to the frontend,
ensuring NO hardcoded pricing values in the frontend code.
"""

from fastapi import APIRouter, HTTPException
from typing import Dict, List, Any
import yaml
import os
from pathlib import Path

router = APIRouter(prefix="/pricing", tags=["pricing"])

# Path to pricing.yaml
PRICING_FILE = Path(__file__).parent / "pricing.yaml"


def load_pricing_config() -> Dict[str, Any]:
    """Load pricing configuration from YAML file."""
    try:
        with open(PRICING_FILE, "r") as f:
            return yaml.safe_load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load pricing config: {e}")


@router.get("/plans")
async def get_plans():
    """
    Get all subscription plans with pricing.
    
    Returns:
        List of plans with credits, pricing, and features
    """
    config = load_pricing_config()
    plans = config.get("plans", {})
    
    return {
        "plans": [
            {
                "id": plan_id,
                "name": plan_data.get("name"),
                "price": plan_data.get("price", {}),
                "credits": plan_data.get("credits", {}),
                "limits": plan_data.get("limits", {}),
            }
            for plan_id, plan_data in plans.items()
        ]
    }


@router.get("/plans/{plan_id}")
async def get_plan(plan_id: str):
    """
    Get specific plan details.
    
    Args:
        plan_id: Plan identifier (developer, plus, enterprise)
    
    Returns:
        Plan details with all configuration
    """
    config = load_pricing_config()
    plans = config.get("plans", {})
    
    plan = plans.get(plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail=f"Plan '{plan_id}' not found")
    
    return {
        "id": plan_id,
        **plan
    }


@router.get("/credit-packs")
async def get_credit_packs():
    """
    Get available credit packs for purchase.
    
    Returns:
        List of credit packs with pricing
    """
    config = load_pricing_config()
    packs = config.get("credit_packs", [])
    
    return {
        "packs": packs
    }


@router.get("/credit-costs")
async def get_credit_costs():
    """
    Get credit costs for various operations.
    
    Returns:
        Credit costs for chat, agents, compute, etc.
    """
    config = load_pricing_config()
    costs = config.get("credit_costs", {})
    
    return {
        "costs": costs,
        "credit_rate": config.get("credit_rate", {})
    }


@router.get("/config")
async def get_full_config():
    """
    Get complete pricing configuration.
    
    Returns:
        Full pricing.yaml content
    """
    config = load_pricing_config()
    return config


@router.get("/tier-mappings")
async def get_tier_mappings():
    """
    Get tier name mappings (for backward compatibility).
    
    Returns:
        Mapping of old tier names to new ones
    """
    config = load_pricing_config()
    return config.get("tier_mappings", {})
