"""
Pricing Configuration Loader

Loads pricing configuration from pricing.yaml as the single source of truth.
This replaces hardcoded values in credit_config.py with YAML-based configuration.
"""

import os
from pathlib import Path
from typing import Dict, Any, Optional
import yaml
from functools import lru_cache


# Path to pricing.yaml
PRICING_YAML_PATH = Path(__file__).parent / "pricing.yaml"


@lru_cache(maxsize=1)
def load_pricing_config() -> Dict[str, Any]:
    """
    Load and cache the pricing configuration from YAML.
    
    Returns:
        Dict containing all pricing configuration
    """
    if not PRICING_YAML_PATH.exists():
        raise FileNotFoundError(f"Pricing config not found: {PRICING_YAML_PATH}")
    
    with open(PRICING_YAML_PATH, "r") as f:
        config = yaml.safe_load(f)
    
    return config


def get_credit_rate() -> float:
    """Get the credit to USD conversion rate."""
    config = load_pricing_config()
    return config.get("credit_rate", {}).get("value", 0.001)


def get_credit_costs() -> Dict[str, Any]:
    """Get all credit cost configurations."""
    config = load_pricing_config()
    return config.get("credit_costs", {})


def get_chat_costs() -> Dict[str, Any]:
    """Get chat/LLM credit costs."""
    return get_credit_costs().get("chat", {})


def get_agent_costs() -> Dict[str, Any]:
    """Get agent execution credit costs."""
    return get_credit_costs().get("agents", {})


def get_compute_costs() -> Dict[str, Any]:
    """Get compute/IDE credit costs."""
    return get_credit_costs().get("compute", {})


def get_workflow_costs() -> Dict[str, Any]:
    """Get workflow credit costs."""
    return get_credit_costs().get("workflows", {})


def get_storage_costs() -> Dict[str, Any]:
    """Get storage/memory credit costs."""
    return get_credit_costs().get("storage", {})


def get_blockchain_costs() -> Dict[str, Any]:
    """Get blockchain audit credit costs."""
    return get_credit_costs().get("blockchain", {})


def get_hash_sphere_costs() -> Dict[str, Any]:
    """Get Hash Sphere credit costs."""
    return get_credit_costs().get("hash_sphere", {})


def get_plans() -> Dict[str, Any]:
    """Get all subscription plan configurations."""
    config = load_pricing_config()
    return config.get("plans", {})


def get_plan(plan_id: str) -> Optional[Dict[str, Any]]:
    """Get a specific plan configuration by ID."""
    plans = get_plans()
    
    # Check tier mappings first
    config = load_pricing_config()
    mappings = config.get("tier_mappings", {})
    mapped_id = mappings.get(plan_id.lower(), plan_id.lower())
    
    return plans.get(mapped_id)


def get_plan_credits(plan_id: str) -> int:
    """Get the included credits for a plan."""
    plan = get_plan(plan_id)
    if plan:
        return plan.get("credits", {}).get("included", 0)
    return 0


def get_plan_credit_rate(plan_id: str) -> float:
    """Get the credit rate multiplier for a plan (discount)."""
    plan = get_plan(plan_id)
    if plan:
        return plan.get("credit_rate", 1.0)
    return 1.0


def get_credit_packs() -> list:
    """Get all credit pack configurations."""
    config = load_pricing_config()
    return config.get("credit_packs", [])


def get_credit_pack(pack_id: str) -> Optional[Dict[str, Any]]:
    """Get a specific credit pack by ID."""
    packs = get_credit_packs()
    for pack in packs:
        if pack.get("id") == pack_id:
            return pack
    return None


def get_provider_multiplier(provider: str) -> float:
    """Get the cost multiplier for a specific LLM provider."""
    chat_costs = get_chat_costs()
    providers = chat_costs.get("providers", {})
    return providers.get(provider.lower(), 1.0)


def get_agent_type_multiplier(agent_type: str) -> float:
    """Get the cost multiplier for a specific agent type."""
    agent_costs = get_agent_costs()
    types = agent_costs.get("types", {})
    return types.get(agent_type.lower(), 1.0)


def get_global_multiplier() -> float:
    """Get the global cost multiplier."""
    config = load_pricing_config()
    return config.get("global", {}).get("multiplier", 1.0)


def get_min_operation_cost() -> int:
    """Get the minimum cost for any operation."""
    config = load_pricing_config()
    return config.get("global", {}).get("min_operation_cost", 1)


def apply_global_multiplier(cost: int) -> int:
    """Apply the global cost multiplier to a cost value."""
    return max(
        get_min_operation_cost(),
        int(cost * get_global_multiplier())
    )


def reload_config():
    """Force reload of the pricing configuration."""
    load_pricing_config.cache_clear()
    return load_pricing_config()


# Convenience exports
__all__ = [
    "load_pricing_config",
    "get_credit_rate",
    "get_credit_costs",
    "get_chat_costs",
    "get_agent_costs",
    "get_compute_costs",
    "get_workflow_costs",
    "get_storage_costs",
    "get_blockchain_costs",
    "get_hash_sphere_costs",
    "get_plans",
    "get_plan",
    "get_plan_credits",
    "get_plan_credit_rate",
    "get_credit_packs",
    "get_credit_pack",
    "get_provider_multiplier",
    "get_agent_type_multiplier",
    "get_global_multiplier",
    "get_min_operation_cost",
    "apply_global_multiplier",
    "reload_config",
]
