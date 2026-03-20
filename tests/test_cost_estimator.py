"""
Tests for Cost Estimator Service - Phase 2.3 GTM

Tests cost estimation for various operations.
"""

import pytest
from unittest.mock import patch, MagicMock

from app.cost_estimator import (
    CostEstimator,
    CostEstimate,
    OperationType,
    ConfidenceLevel,
    cost_estimator,
    estimate_chat_cost,
    estimate_agent_cost,
    estimate_workflow_cost,
)


class TestOperationType:
    """Test OperationType enum."""
    
    def test_operation_types(self):
        """Test all operation types are defined."""
        assert OperationType.CHAT.value == "chat"
        assert OperationType.AGENT.value == "agent"
        assert OperationType.WORKFLOW.value == "workflow"
        assert OperationType.COMPUTE.value == "compute"
        assert OperationType.CODE_EXECUTION.value == "code_execution"


class TestConfidenceLevel:
    """Test ConfidenceLevel enum."""
    
    def test_confidence_levels(self):
        """Test all confidence levels are defined."""
        assert ConfidenceLevel.HIGH.value == "high"
        assert ConfidenceLevel.MEDIUM.value == "medium"
        assert ConfidenceLevel.LOW.value == "low"


class TestCostEstimate:
    """Test CostEstimate dataclass."""
    
    def test_to_dict(self):
        """Test to_dict method."""
        estimate = CostEstimate(
            estimated_cost=100,
            min_cost=50,
            max_cost=200,
            confidence="medium",
            operation_type="chat",
            breakdown={"input_tokens": 100},
            notes=["Test note"],
        )
        
        d = estimate.to_dict()
        
        assert d["estimated_cost"] == 100
        assert d["min_cost"] == 50
        assert d["max_cost"] == 200
        assert d["confidence"] == "medium"
        assert d["operation_type"] == "chat"
        assert d["breakdown"]["input_tokens"] == 100
        assert "Test note" in d["notes"]


class TestChatEstimation:
    """Test chat cost estimation."""
    
    def setup_method(self):
        self.estimator = CostEstimator()
    
    def test_estimate_chat_basic(self):
        """Test basic chat estimation."""
        estimate = self.estimator.estimate_chat(
            message_length=100,
            model="gpt-4o",
            provider="openai",
        )
        
        assert isinstance(estimate, CostEstimate)
        assert estimate.estimated_cost > 0
        assert estimate.min_cost <= estimate.estimated_cost
        assert estimate.max_cost >= estimate.estimated_cost
        assert estimate.confidence == ConfidenceLevel.MEDIUM.value
        assert estimate.operation_type == OperationType.CHAT.value
    
    def test_estimate_chat_longer_message(self):
        """Test longer messages cost more."""
        short = self.estimator.estimate_chat(message_length=100)
        long = self.estimator.estimate_chat(message_length=1000)
        
        assert long.estimated_cost > short.estimated_cost
    
    def test_estimate_chat_provider_multipliers(self):
        """Test provider multipliers affect cost."""
        openai = self.estimator.estimate_chat(
            message_length=500,
            provider="openai",
        )
        anthropic = self.estimator.estimate_chat(
            message_length=500,
            provider="anthropic",
        )
        groq = self.estimator.estimate_chat(
            message_length=500,
            provider="groq",
        )
        
        # Anthropic is more expensive (1.2x)
        assert anthropic.estimated_cost > openai.estimated_cost
        
        # Groq is cheaper (0.5x)
        assert groq.estimated_cost < openai.estimated_cost
    
    def test_estimate_chat_breakdown(self):
        """Test breakdown contains expected fields."""
        estimate = self.estimator.estimate_chat(message_length=200)
        
        assert "input_tokens" in estimate.breakdown
        assert "estimated_output_tokens" in estimate.breakdown
        assert "message_tokens" in estimate.breakdown
        assert "model" in estimate.breakdown
        assert "provider" in estimate.breakdown
        assert "multiplier" in estimate.breakdown
    
    def test_estimate_chat_notes(self):
        """Test notes are provided."""
        estimate = self.estimator.estimate_chat(message_length=100)
        
        assert len(estimate.notes) > 0
        assert any("response length" in note.lower() for note in estimate.notes)


class TestAgentEstimation:
    """Test agent run cost estimation."""
    
    def setup_method(self):
        self.estimator = CostEstimator()
    
    def test_estimate_agent_basic(self):
        """Test basic agent estimation."""
        estimate = self.estimator.estimate_agent_run(
            agent_type="general",
            estimated_steps=5,
        )
        
        assert isinstance(estimate, CostEstimate)
        assert estimate.estimated_cost > 0
        assert estimate.confidence == ConfidenceLevel.LOW.value
        assert estimate.operation_type == OperationType.AGENT.value
    
    def test_estimate_agent_more_steps(self):
        """Test more steps cost more."""
        few = self.estimator.estimate_agent_run(estimated_steps=3)
        many = self.estimator.estimate_agent_run(estimated_steps=10)
        
        assert many.estimated_cost > few.estimated_cost
    
    def test_estimate_agent_complexity(self):
        """Test complexity affects cost."""
        low = self.estimator.estimate_agent_run(complexity="low")
        high = self.estimator.estimate_agent_run(complexity="high")
        
        assert high.estimated_cost > low.estimated_cost
    
    def test_estimate_agent_tools(self):
        """Test tool usage adds overhead."""
        without_tools = self.estimator.estimate_agent_run(include_tools=False)
        with_tools = self.estimator.estimate_agent_run(include_tools=True)
        
        assert with_tools.estimated_cost > without_tools.estimated_cost
    
    def test_estimate_agent_breakdown(self):
        """Test breakdown contains expected fields."""
        estimate = self.estimator.estimate_agent_run()
        
        assert "session_cost" in estimate.breakdown
        assert "step_cost" in estimate.breakdown
        assert "estimated_steps" in estimate.breakdown
        assert "agent_type" in estimate.breakdown
        assert "complexity" in estimate.breakdown


class TestWorkflowEstimation:
    """Test workflow cost estimation."""
    
    def setup_method(self):
        self.estimator = CostEstimator()
    
    def test_estimate_workflow_basic(self):
        """Test basic workflow estimation."""
        estimate = self.estimator.estimate_workflow(node_count=5)
        
        assert isinstance(estimate, CostEstimate)
        assert estimate.estimated_cost > 0
        assert estimate.confidence == ConfidenceLevel.HIGH.value
        assert estimate.operation_type == OperationType.WORKFLOW.value
    
    def test_estimate_workflow_more_nodes(self):
        """Test more nodes cost more."""
        few = self.estimator.estimate_workflow(node_count=3)
        many = self.estimator.estimate_workflow(node_count=10)
        
        assert many.estimated_cost > few.estimated_cost
    
    def test_estimate_workflow_parallel(self):
        """Test parallel branches add cost."""
        sequential = self.estimator.estimate_workflow(node_count=5, has_parallel=False)
        parallel = self.estimator.estimate_workflow(node_count=5, has_parallel=True)
        
        assert parallel.estimated_cost > sequential.estimated_cost
    
    def test_estimate_workflow_loops(self):
        """Test loops add cost."""
        no_loops = self.estimator.estimate_workflow(node_count=5, has_loops=False)
        with_loops = self.estimator.estimate_workflow(node_count=5, has_loops=True)
        
        assert with_loops.estimated_cost > no_loops.estimated_cost
    
    def test_estimate_workflow_iterations(self):
        """Test more iterations cost more."""
        one = self.estimator.estimate_workflow(node_count=5, estimated_iterations=1)
        three = self.estimator.estimate_workflow(node_count=5, estimated_iterations=3)
        
        assert three.estimated_cost > one.estimated_cost


class TestCodeExecutionEstimation:
    """Test code execution cost estimation."""
    
    def setup_method(self):
        self.estimator = CostEstimator()
    
    def test_estimate_code_basic(self):
        """Test basic code execution estimation."""
        estimate = self.estimator.estimate_code_execution(estimated_seconds=10)
        
        assert isinstance(estimate, CostEstimate)
        assert estimate.estimated_cost > 0
        assert estimate.confidence == ConfidenceLevel.MEDIUM.value
        assert estimate.operation_type == OperationType.CODE_EXECUTION.value
    
    def test_estimate_code_longer_duration(self):
        """Test longer execution costs more."""
        short = self.estimator.estimate_code_execution(estimated_seconds=5)
        long = self.estimator.estimate_code_execution(estimated_seconds=60)
        
        assert long.estimated_cost > short.estimated_cost
    
    def test_estimate_code_more_memory(self):
        """Test more memory costs more."""
        low_mem = self.estimator.estimate_code_execution(memory_mb=256)
        high_mem = self.estimator.estimate_code_execution(memory_mb=1024)
        
        assert high_mem.estimated_cost > low_mem.estimated_cost


class TestBatchEstimation:
    """Test batch cost estimation."""
    
    def setup_method(self):
        self.estimator = CostEstimator()
    
    def test_estimate_batch_single(self):
        """Test batch with single operation."""
        result = self.estimator.estimate_batch([
            {"type": "chat", "message_length": 100},
        ])
        
        assert result["operation_count"] == 1
        assert result["total_estimated"] > 0
        assert len(result["estimates"]) == 1
    
    def test_estimate_batch_multiple(self):
        """Test batch with multiple operations."""
        result = self.estimator.estimate_batch([
            {"type": "chat", "message_length": 100},
            {"type": "agent", "estimated_steps": 5},
            {"type": "workflow", "node_count": 3},
        ])
        
        assert result["operation_count"] == 3
        assert result["total_estimated"] > 0
        assert len(result["estimates"]) == 3
    
    def test_estimate_batch_totals(self):
        """Test batch totals are sum of individual estimates."""
        result = self.estimator.estimate_batch([
            {"type": "chat", "message_length": 100},
            {"type": "chat", "message_length": 200},
        ])
        
        individual_sum = sum(e["estimated_cost"] for e in result["estimates"])
        assert result["total_estimated"] == individual_sum


class TestConvenienceFunctions:
    """Test convenience functions."""
    
    def test_estimate_chat_cost(self):
        """Test estimate_chat_cost function."""
        result = estimate_chat_cost(message_length=100)
        
        assert "estimated_cost" in result
        assert "breakdown" in result
    
    def test_estimate_agent_cost(self):
        """Test estimate_agent_cost function."""
        result = estimate_agent_cost(agent_type="general")
        
        assert "estimated_cost" in result
        assert "breakdown" in result
    
    def test_estimate_workflow_cost(self):
        """Test estimate_workflow_cost function."""
        result = estimate_workflow_cost(node_count=5)
        
        assert "estimated_cost" in result
        assert "breakdown" in result


class TestGlobalEstimator:
    """Test global estimator instance."""
    
    def test_global_instance_exists(self):
        """Test global instance exists."""
        assert cost_estimator is not None
        assert isinstance(cost_estimator, CostEstimator)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
