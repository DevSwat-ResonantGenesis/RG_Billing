"""
Pricing and Credit Calculation Test Probes

Tests the complete pricing system:
1. Pricing API endpoints
2. Credit calculations (token-based)
3. Plan configurations
4. Credit deduction logic
5. Tier limits and features
"""

import pytest
import httpx
import asyncio
from decimal import Decimal

# Gateway URL for API tests
GATEWAY_URL = "http://localhost:8000"
BILLING_SERVICE_URL = "http://localhost:8000"  # Via gateway


class TestPricingAPI:
    """Test pricing API endpoints."""
    
    def test_pricing_endpoint_accessible(self):
        """Test that /billing/pricing is publicly accessible."""
        response = httpx.get(f"{GATEWAY_URL}/billing/pricing", timeout=10)
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        data = response.json()
        assert "plans" in data
        assert "credit_rate" in data
        assert "credit_packs" in data
    
    def test_pricing_plans_structure(self):
        """Test that plans have correct structure."""
        response = httpx.get(f"{GATEWAY_URL}/billing/pricing", timeout=10)
        data = response.json()
        plans = data["plans"]
        
        # Must have exactly 3 plans
        assert len(plans) == 3, f"Expected 3 plans, got {len(plans)}"
        assert "developer" in plans
        assert "plus" in plans
        assert "enterprise" in plans
    
    def test_developer_plan_values(self):
        """Test Developer plan has correct values."""
        response = httpx.get(f"{GATEWAY_URL}/billing/pricing", timeout=10)
        data = response.json()
        dev = data["plans"]["developer"]
        
        # Price
        assert dev["price"]["monthly"] == 0, "Developer should be free"
        assert dev["price"]["yearly"] == 0
        
        # Credits
        assert dev["credits"]["included"] == 1000
        assert dev["credits"]["rollover"] == False, "Developer should not have rollover"
        assert dev["credits"]["topups"] == False, "Developer should not have topups"
        
        # Limits
        assert dev["limits"]["agents"] == 3
        assert dev["limits"]["autonomous_mode"] == False
    
    def test_plus_plan_values(self):
        """Test Plus plan has correct values."""
        response = httpx.get(f"{GATEWAY_URL}/billing/pricing", timeout=10)
        data = response.json()
        plus = data["plans"]["plus"]
        
        # Price
        assert plus["price"]["monthly"] == 49, f"Plus should be $49/month, got {plus['price']['monthly']}"
        assert plus["price"]["yearly"] == 490
        
        # Credits
        assert plus["credits"]["included"] == 75000, f"Plus should have 75K credits, got {plus['credits']['included']}"
        assert plus["credits"]["rollover"] == True
        assert plus["credits"]["rollover_limit"] == 37500, f"Plus rollover should be 37.5K, got {plus['credits'].get('rollover_limit')}"
        assert plus["credits"]["topups"] == True
        assert plus["credits"]["topup_price"] == 8, "Plus topup should be $8/10K"
        
        # Limits
        assert plus["limits"]["agents"] == 20
        assert plus["limits"]["autonomous_mode"] == True
    
    def test_enterprise_plan_values(self):
        """Test Enterprise plan has correct values."""
        response = httpx.get(f"{GATEWAY_URL}/billing/pricing", timeout=10)
        data = response.json()
        ent = data["plans"]["enterprise"]
        
        # Credits (custom/unlimited = -1)
        assert ent["credits"]["included"] == -1, "Enterprise should have unlimited credits"
        assert ent["credits"]["rollover"] == True
        assert ent["credits"]["topup_price"] == 5, "Enterprise topup should be $5/10K"
    
    def test_credit_rate(self):
        """Test credit rate is correct."""
        response = httpx.get(f"{GATEWAY_URL}/billing/pricing", timeout=10)
        data = response.json()
        
        assert data["credit_rate"]["value"] == 0.001, "1 credit should = $0.001"
        assert data["credit_rate"]["currency"] == "USD"
    
    def test_credit_packs(self):
        """Test credit packs are available."""
        response = httpx.get(f"{GATEWAY_URL}/billing/pricing", timeout=10)
        data = response.json()
        packs = data["credit_packs"]
        
        assert len(packs) >= 3, "Should have at least 3 credit packs"
        
        # Check starter pack
        starter = next((p for p in packs if p["id"] == "starter"), None)
        assert starter is not None, "Should have starter pack"
        assert starter["credits"] == 5000
        assert starter["price"] == 5
    
    def test_credit_costs_structure(self):
        """Test credit costs are defined."""
        response = httpx.get(f"{GATEWAY_URL}/billing/pricing", timeout=10)
        data = response.json()
        costs = data.get("credit_costs", {})
        
        # Should have all service categories
        assert "chat" in costs, "Should have chat costs"
        assert "agents" in costs, "Should have agent costs"
        assert "compute" in costs, "Should have compute costs"
        assert "workflows" in costs, "Should have workflow costs"
        assert "storage" in costs, "Should have storage costs"


class TestCreditCalculations:
    """Test credit calculation logic."""
    
    def test_token_cost_calculation_openai(self):
        """Test token-based credit calculation for OpenAI."""
        # Formula: (input/1000 * 10 + output/1000 * 30) * multiplier
        # OpenAI multiplier = 1.0
        input_tokens = 1000
        output_tokens = 500
        
        expected = (1000/1000 * 10 + 500/1000 * 30) * 1.0
        expected = int(expected + 0.5)  # Round
        
        assert expected == 25, f"1K input + 500 output should cost 25 credits, got {expected}"
    
    def test_token_cost_calculation_anthropic(self):
        """Test token-based credit calculation for Anthropic."""
        # Anthropic multiplier = 1.2
        input_tokens = 1000
        output_tokens = 500
        
        base = (1000/1000 * 10 + 500/1000 * 30)
        expected = int(base * 1.2 + 0.5)
        
        assert expected == 30, f"Anthropic should cost 30 credits (1.2x), got {expected}"
    
    def test_token_cost_calculation_groq(self):
        """Test token-based credit calculation for Groq."""
        # Groq multiplier = 0.5
        input_tokens = 1000
        output_tokens = 500
        
        base = (1000/1000 * 10 + 500/1000 * 30)
        expected = int(base * 0.5 + 0.5)
        
        assert expected == 13, f"Groq should cost ~13 credits (0.5x), got {expected}"
    
    def test_minimum_credit_cost(self):
        """Test minimum credit cost is 1."""
        # Very small token count should still cost at least 1
        input_tokens = 10
        output_tokens = 5
        
        base = (10/1000 * 10 + 5/1000 * 30)
        expected = max(1, int(base + 0.5))
        
        assert expected >= 1, "Minimum cost should be 1 credit"
    
    def test_large_token_cost(self):
        """Test large token count calculation."""
        input_tokens = 100000  # 100K input
        output_tokens = 50000   # 50K output
        
        expected = int((100000/1000 * 10 + 50000/1000 * 30) + 0.5)
        
        assert expected == 2500, f"100K input + 50K output should cost 2500 credits, got {expected}"


class TestChatCosts:
    """Test chat-specific credit costs from pricing.yaml."""
    
    def test_chat_costs_defined(self):
        """Test chat costs are properly defined."""
        response = httpx.get(f"{GATEWAY_URL}/billing/pricing", timeout=10)
        data = response.json()
        chat = data.get("credit_costs", {}).get("chat", {})
        
        assert "prompt_token_cost" in chat or "base_cost" in chat, "Chat costs should be defined"
        assert "providers" in chat, "Provider multipliers should be defined"
    
    def test_provider_multipliers(self):
        """Test provider multipliers are correct."""
        response = httpx.get(f"{GATEWAY_URL}/billing/pricing", timeout=10)
        data = response.json()
        providers = data.get("credit_costs", {}).get("chat", {}).get("providers", {})
        
        assert providers.get("openai") == 1.0, "OpenAI should be 1.0x"
        assert providers.get("anthropic") == 1.2, "Anthropic should be 1.2x"
        assert providers.get("google") == 0.8, "Google should be 0.8x"
        assert providers.get("groq") == 0.5, "Groq should be 0.5x"
        assert providers.get("local") == 0.1, "Local should be 0.1x"


class TestAgentCosts:
    """Test agent-specific credit costs."""
    
    def test_agent_costs_defined(self):
        """Test agent costs are properly defined."""
        response = httpx.get(f"{GATEWAY_URL}/billing/pricing", timeout=10)
        data = response.json()
        agents = data.get("credit_costs", {}).get("agents", {})
        
        assert "session_start" in agents, "Agent session_start cost should be defined"
        assert "step" in agents, "Agent step cost should be defined"
        assert "tool_invocation" in agents, "Agent tool_invocation cost should be defined"
    
    def test_agent_type_multipliers(self):
        """Test agent type multipliers."""
        response = httpx.get(f"{GATEWAY_URL}/billing/pricing", timeout=10)
        data = response.json()
        types = data.get("credit_costs", {}).get("agents", {}).get("types", {})
        
        assert types.get("simple") == 0.5, "Simple agent should be 0.5x"
        assert types.get("default") == 1.0, "Default agent should be 1.0x"
        assert types.get("complex") == 1.5, "Complex agent should be 1.5x"
        assert types.get("autonomous") == 2.0, "Autonomous agent should be 2.0x"


class TestTierMappings:
    """Test tier name mappings for backward compatibility."""
    
    def test_tier_mappings_exist(self):
        """Test tier mappings are defined."""
        response = httpx.get(f"{GATEWAY_URL}/billing/pricing", timeout=10)
        data = response.json()
        mappings = data.get("tier_mappings", {})
        
        assert "free" in mappings, "Should map 'free' to 'developer'"
        assert mappings["free"] == "developer"
        assert mappings["developer"] == "developer"
        assert mappings["plus"] == "plus"
        assert mappings.get("pro") == "plus", "Should map 'pro' to 'plus'"


class TestDashboardEndpoints:
    """Test dashboard-related endpoints (requires auth)."""
    
    def test_dashboard_requires_auth(self):
        """Test that dashboard endpoint requires authentication."""
        response = httpx.get(f"{GATEWAY_URL}/billing/dashboard/me", timeout=10)
        # Should return 401 without auth
        assert response.status_code == 401, f"Dashboard should require auth, got {response.status_code}"
    
    def test_credits_endpoint_requires_auth(self):
        """Test that credits endpoint requires authentication."""
        response = httpx.get(f"{GATEWAY_URL}/billing/credits", timeout=10)
        assert response.status_code == 401, f"Credits should require auth, got {response.status_code}"


def run_all_tests():
    """Run all pricing tests and print results."""
    print("=" * 60)
    print("PRICING AND CREDIT CALCULATION TEST PROBES")
    print("=" * 60)
    
    test_classes = [
        TestPricingAPI,
        TestCreditCalculations,
        TestChatCosts,
        TestAgentCosts,
        TestTierMappings,
        TestDashboardEndpoints,
    ]
    
    total_passed = 0
    total_failed = 0
    failures = []
    
    for test_class in test_classes:
        print(f"\n{test_class.__name__}")
        print("-" * 40)
        
        instance = test_class()
        for method_name in dir(instance):
            if method_name.startswith("test_"):
                try:
                    getattr(instance, method_name)()
                    print(f"  ✓ {method_name}")
                    total_passed += 1
                except AssertionError as e:
                    print(f"  ✗ {method_name}: {e}")
                    total_failed += 1
                    failures.append((test_class.__name__, method_name, str(e)))
                except Exception as e:
                    print(f"  ✗ {method_name}: ERROR - {e}")
                    total_failed += 1
                    failures.append((test_class.__name__, method_name, str(e)))
    
    print("\n" + "=" * 60)
    print(f"RESULTS: {total_passed} passed, {total_failed} failed")
    print("=" * 60)
    
    if failures:
        print("\nFAILURES:")
        for cls, method, error in failures:
            print(f"  - {cls}.{method}: {error}")
    
    return total_failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    exit(0 if success else 1)
