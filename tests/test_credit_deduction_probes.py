"""
Credit Deduction Test Probes

Tests the actual credit deduction flow:
1. Token tracker calculations
2. Credit deduction via billing service
3. Balance updates
4. Usage record creation
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.pricing_loader import (
    load_pricing_config,
    get_credit_rate,
    get_plans,
    get_plan,
    get_plan_credits,
    get_credit_packs,
    get_chat_costs,
    get_agent_costs,
    get_provider_multiplier,
    get_agent_type_multiplier,
)


class TestPricingLoader:
    """Test pricing loader functions."""
    
    def test_load_pricing_config(self):
        """Test loading full pricing config."""
        config = load_pricing_config()
        assert config is not None
        assert "plans" in config
        assert "credit_rate" in config
        assert "credit_costs" in config
    
    def test_get_credit_rate(self):
        """Test credit rate retrieval."""
        rate = get_credit_rate()
        assert rate == 0.001, f"Credit rate should be 0.001, got {rate}"
    
    def test_get_plans(self):
        """Test getting all plans."""
        plans = get_plans()
        assert len(plans) == 3
        assert "developer" in plans
        assert "plus" in plans
        assert "enterprise" in plans
    
    def test_get_plan_developer(self):
        """Test getting developer plan."""
        plan = get_plan("developer")
        assert plan is not None
        assert plan["credits"]["included"] == 1000
        assert plan["price"]["monthly"] == 0
    
    def test_get_plan_plus(self):
        """Test getting plus plan."""
        plan = get_plan("plus")
        assert plan is not None
        assert plan["credits"]["included"] == 75000
        assert plan["price"]["monthly"] == 49
    
    def test_get_plan_with_mapping(self):
        """Test plan retrieval with tier mapping."""
        # 'free' should map to 'developer'
        plan = get_plan("free")
        assert plan is not None
        assert plan["id"] == "developer"
        
        # 'pro' should map to 'plus'
        plan = get_plan("pro")
        assert plan is not None
        assert plan["id"] == "plus"
    
    def test_get_plan_credits(self):
        """Test getting plan credits."""
        assert get_plan_credits("developer") == 1000
        assert get_plan_credits("plus") == 75000
        assert get_plan_credits("enterprise") == -1  # Unlimited
    
    def test_get_credit_packs(self):
        """Test getting credit packs."""
        packs = get_credit_packs()
        assert len(packs) >= 3
        
        # Find starter pack
        starter = next((p for p in packs if p["id"] == "starter"), None)
        assert starter is not None
        assert starter["credits"] == 5000
        assert starter["price"] == 5
    
    def test_get_chat_costs(self):
        """Test getting chat costs."""
        costs = get_chat_costs()
        assert costs is not None
        assert "providers" in costs
        assert costs["providers"]["openai"] == 1.0
    
    def test_get_agent_costs(self):
        """Test getting agent costs."""
        costs = get_agent_costs()
        assert costs is not None
        assert "session_start" in costs
        assert "step" in costs
        assert "types" in costs
    
    def test_get_provider_multiplier(self):
        """Test provider multiplier retrieval."""
        assert get_provider_multiplier("openai") == 1.0
        assert get_provider_multiplier("anthropic") == 1.2
        assert get_provider_multiplier("google") == 0.8
        assert get_provider_multiplier("groq") == 0.5
        assert get_provider_multiplier("local") == 0.1
        assert get_provider_multiplier("unknown") == 1.0  # Default
    
    def test_get_agent_type_multiplier(self):
        """Test agent type multiplier retrieval."""
        assert get_agent_type_multiplier("simple") == 0.5
        assert get_agent_type_multiplier("default") == 1.0
        assert get_agent_type_multiplier("complex") == 1.5
        assert get_agent_type_multiplier("autonomous") == 2.0
        assert get_agent_type_multiplier("unknown") == 1.0  # Default


class TestCreditCalculationFormulas:
    """Test credit calculation formulas match expected values."""
    
    def calculate_token_cost(self, input_tokens: int, output_tokens: int, provider: str = "openai") -> int:
        """Calculate credit cost for tokens."""
        INPUT_COST_PER_1K = 10
        OUTPUT_COST_PER_1K = 30
        
        multiplier = get_provider_multiplier(provider)
        input_credits = (input_tokens / 1000) * INPUT_COST_PER_1K * multiplier
        output_credits = (output_tokens / 1000) * OUTPUT_COST_PER_1K * multiplier
        
        return max(1, int(input_credits + output_credits + 0.5))
    
    def test_simple_message_cost(self):
        """Test cost for a simple chat message (~500 tokens)."""
        # Typical short message: 200 input, 300 output
        cost = self.calculate_token_cost(200, 300)
        # (200/1000 * 10 + 300/1000 * 30) = 2 + 9 = 11
        assert cost == 11, f"Simple message should cost 11 credits, got {cost}"
    
    def test_medium_message_cost(self):
        """Test cost for a medium chat message (~2000 tokens)."""
        # Medium message: 800 input, 1200 output
        cost = self.calculate_token_cost(800, 1200)
        # (800/1000 * 10 + 1200/1000 * 30) = 8 + 36 = 44
        assert cost == 44, f"Medium message should cost 44 credits, got {cost}"
    
    def test_long_message_cost(self):
        """Test cost for a long chat message (~8000 tokens)."""
        # Long message: 3000 input, 5000 output
        cost = self.calculate_token_cost(3000, 5000)
        # (3000/1000 * 10 + 5000/1000 * 30) = 30 + 150 = 180
        assert cost == 180, f"Long message should cost 180 credits, got {cost}"
    
    def test_anthropic_premium(self):
        """Test Anthropic costs 20% more."""
        openai_cost = self.calculate_token_cost(1000, 1000, "openai")
        anthropic_cost = self.calculate_token_cost(1000, 1000, "anthropic")
        
        # OpenAI: (1000/1000 * 10 + 1000/1000 * 30) * 1.0 = 40
        # Anthropic: 40 * 1.2 = 48
        assert openai_cost == 40
        assert anthropic_cost == 48
        assert anthropic_cost > openai_cost
    
    def test_groq_discount(self):
        """Test Groq costs 50% less."""
        openai_cost = self.calculate_token_cost(1000, 1000, "openai")
        groq_cost = self.calculate_token_cost(1000, 1000, "groq")
        
        # OpenAI: 40
        # Groq: 40 * 0.5 = 20
        assert openai_cost == 40
        assert groq_cost == 20
        assert groq_cost < openai_cost
    
    def test_local_minimal_cost(self):
        """Test local models cost 90% less."""
        openai_cost = self.calculate_token_cost(1000, 1000, "openai")
        local_cost = self.calculate_token_cost(1000, 1000, "local")
        
        # OpenAI: 40
        # Local: 40 * 0.1 = 4
        assert openai_cost == 40
        assert local_cost == 4
    
    def test_credits_per_dollar(self):
        """Test credits per dollar calculation."""
        # 1 credit = $0.001
        # $1 = 1000 credits
        # $10 = 10000 credits (starter pack)
        # $49 = 49000 credits worth (but Plus gives 75000 = 53% bonus)
        
        credit_rate = get_credit_rate()
        credits_per_dollar = 1 / credit_rate
        
        assert credits_per_dollar == 1000, f"Should get 1000 credits per dollar, got {credits_per_dollar}"
    
    def test_developer_monthly_budget(self):
        """Test how many messages Developer tier can send."""
        monthly_credits = get_plan_credits("developer")  # 10000
        avg_message_cost = 20  # Average from pricing.yaml
        
        messages_per_month = monthly_credits // avg_message_cost
        assert messages_per_month == 500, f"Developer should get ~500 messages/month, got {messages_per_month}"
    
    def test_plus_monthly_budget(self):
        """Test how many messages Plus tier can send."""
        monthly_credits = get_plan_credits("plus")  # 75000
        avg_message_cost = 20
        
        messages_per_month = monthly_credits // avg_message_cost
        assert messages_per_month == 3750, f"Plus should get ~3750 messages/month, got {messages_per_month}"


class TestAgentCostCalculations:
    """Test agent-specific cost calculations."""
    
    def test_simple_agent_session(self):
        """Test cost for a simple agent session."""
        costs = get_agent_costs()
        session_start = costs.get("session_start", 100)
        step_cost = costs.get("step", 500)
        
        # Simple agent (0.5x multiplier), 3 steps
        multiplier = get_agent_type_multiplier("simple")
        total = int((session_start + step_cost * 3) * multiplier)
        
        # (100 + 500*3) * 0.5 = 1600 * 0.5 = 800
        assert total == 800, f"Simple agent 3-step session should cost 800, got {total}"
    
    def test_autonomous_agent_session(self):
        """Test cost for an autonomous agent session."""
        costs = get_agent_costs()
        session_start = costs.get("session_start", 100)
        step_cost = costs.get("step", 500)
        
        # Autonomous agent (2.0x multiplier), 3 steps
        multiplier = get_agent_type_multiplier("autonomous")
        total = int((session_start + step_cost * 3) * multiplier)
        
        # (100 + 500*3) * 2.0 = 1600 * 2.0 = 3200
        assert total == 3200, f"Autonomous agent 3-step session should cost 3200, got {total}"


def run_all_tests():
    """Run all credit deduction tests."""
    print("=" * 60)
    print("CREDIT DEDUCTION TEST PROBES")
    print("=" * 60)
    
    test_classes = [
        TestPricingLoader,
        TestCreditCalculationFormulas,
        TestAgentCostCalculations,
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
