"""
Cost Estimation API - Phase 2.3 GTM

Estimate costs before execution to help users plan their usage.
"""

import logging
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, asdict
from enum import Enum

from .pricing_loader import (
    get_chat_costs,
    get_agent_costs,
    get_workflow_costs,
    get_compute_costs,
)

logger = logging.getLogger(__name__)


class OperationType(str, Enum):
    """Supported operation types for estimation."""
    CHAT = "chat"
    AGENT = "agent"
    WORKFLOW = "workflow"
    COMPUTE = "compute"
    CODE_EXECUTION = "code_execution"


class ConfidenceLevel(str, Enum):
    """Confidence level for estimates."""
    HIGH = "high"      # Predictable operations (workflows, compute)
    MEDIUM = "medium"  # Somewhat predictable (chat)
    LOW = "low"        # Highly variable (agents)


@dataclass
class CostEstimate:
    """Cost estimation result."""
    estimated_cost: int
    min_cost: int
    max_cost: int
    confidence: str
    operation_type: str
    breakdown: Dict[str, Any]
    notes: List[str]
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class CostEstimator:
    """
    Estimate costs before execution.
    
    Provides cost estimates for various operations to help users
    understand and plan their credit usage.
    """
    
    # Provider multipliers (from pricing.yaml)
    PROVIDER_MULTIPLIERS = {
        "openai": 1.0,
        "anthropic": 1.2,
        "google": 0.8,
        "groq": 0.5,
        "local": 0.1,
    }
    
    # Average tokens per character (rough estimate)
    CHARS_PER_TOKEN = 4
    
    # Average response lengths by model
    AVG_OUTPUT_TOKENS = {
        "gpt-4o": 500,
        "gpt-4o-mini": 400,
        "gpt-4": 500,
        "gpt-3.5-turbo": 300,
        "claude-3-opus": 600,
        "claude-3-sonnet": 500,
        "claude-3-haiku": 300,
        "gemini-pro": 400,
    }
    
    def estimate_chat(
        self,
        message_length: int,
        model: str = "gpt-4o",
        provider: str = "openai",
        include_context: bool = True,
        context_messages: int = 10,
    ) -> CostEstimate:
        """
        Estimate cost for a chat message.
        
        Args:
            message_length: Length of user message in characters
            model: LLM model name
            provider: LLM provider
            include_context: Whether to include context messages
            context_messages: Number of context messages
            
        Returns:
            CostEstimate with breakdown
        """
        # Estimate input tokens
        message_tokens = max(1, message_length // self.CHARS_PER_TOKEN)
        
        # Add context overhead
        context_tokens = 0
        if include_context:
            # Assume ~200 tokens per context message on average
            context_tokens = context_messages * 200
        
        # System prompt overhead (~100 tokens)
        system_tokens = 100
        
        total_input = message_tokens + context_tokens + system_tokens
        
        # Estimate output tokens
        estimated_output = self.AVG_OUTPUT_TOKENS.get(model, 500)
        
        # Get multiplier
        multiplier = self.PROVIDER_MULTIPLIERS.get(provider.lower(), 1.0)
        
        # Calculate costs (10 credits per 1K input, 30 per 1K output)
        input_cost = (total_input / 1000) * 10 * multiplier
        output_cost = (estimated_output / 1000) * 30 * multiplier
        
        estimated_cost = max(1, int(input_cost + output_cost + 0.5))
        
        # Calculate range (output can vary significantly)
        min_output = estimated_output // 2
        max_output = estimated_output * 3
        
        min_cost = max(1, int((total_input / 1000) * 10 * multiplier + (min_output / 1000) * 30 * multiplier))
        max_cost = int((total_input / 1000) * 10 * multiplier + (max_output / 1000) * 30 * multiplier + 0.5)
        
        return CostEstimate(
            estimated_cost=estimated_cost,
            min_cost=min_cost,
            max_cost=max_cost,
            confidence=ConfidenceLevel.MEDIUM.value,
            operation_type=OperationType.CHAT.value,
            breakdown={
                "input_tokens": total_input,
                "estimated_output_tokens": estimated_output,
                "message_tokens": message_tokens,
                "context_tokens": context_tokens,
                "system_tokens": system_tokens,
                "model": model,
                "provider": provider,
                "multiplier": multiplier,
            },
            notes=[
                "Actual cost depends on response length",
                f"Estimate assumes ~{estimated_output} output tokens",
                "Complex questions may generate longer responses",
            ],
        )
    
    def estimate_agent_run(
        self,
        agent_type: str = "general",
        estimated_steps: int = 5,
        include_tools: bool = True,
        complexity: str = "medium",
    ) -> CostEstimate:
        """
        Estimate cost for an agent run.
        
        Args:
            agent_type: Type of agent (general, code, research, etc.)
            estimated_steps: Expected number of reasoning steps
            include_tools: Whether agent uses tools
            complexity: Task complexity (low, medium, high)
            
        Returns:
            CostEstimate with breakdown
        """
        try:
            costs = get_agent_costs()
        except:
            # Fallback defaults
            costs = {
                "session_start": 100,
                "step": 50,
                "goal_completion": 200,
                "types": {"general": 1.0, "code": 1.2, "research": 1.5},
            }
        
        session_cost = costs.get("session_start", 100)
        step_cost = costs.get("step", 50)
        goal_cost = costs.get("goal_completion", 200)
        
        # Agent type multiplier
        type_multipliers = costs.get("types", {})
        type_multiplier = type_multipliers.get(agent_type, 1.0)
        
        # Complexity multiplier
        complexity_multipliers = {"low": 0.7, "medium": 1.0, "high": 1.5}
        complexity_multiplier = complexity_multipliers.get(complexity, 1.0)
        
        # Tool usage adds overhead
        tool_overhead = 1.2 if include_tools else 1.0
        
        # Calculate base cost
        base_cost = session_cost + (step_cost * estimated_steps) + goal_cost
        
        # Apply multipliers
        estimated_cost = int(base_cost * type_multiplier * complexity_multiplier * tool_overhead)
        
        # Agent runs are highly variable
        min_cost = int(session_cost + (step_cost * 2))  # Minimum viable run
        max_cost = int(base_cost * type_multiplier * 2.0 * tool_overhead)  # Could take 2x steps
        
        return CostEstimate(
            estimated_cost=estimated_cost,
            min_cost=min_cost,
            max_cost=max_cost,
            confidence=ConfidenceLevel.LOW.value,
            operation_type=OperationType.AGENT.value,
            breakdown={
                "session_cost": session_cost,
                "step_cost": step_cost,
                "estimated_steps": estimated_steps,
                "goal_cost": goal_cost,
                "agent_type": agent_type,
                "type_multiplier": type_multiplier,
                "complexity": complexity,
                "complexity_multiplier": complexity_multiplier,
                "includes_tools": include_tools,
            },
            notes=[
                "Agent runs vary significantly based on task complexity",
                f"Estimate assumes {estimated_steps} reasoning steps",
                "Complex tasks may require more steps",
                "Tool usage adds ~20% overhead",
            ],
        )
    
    def estimate_workflow(
        self,
        node_count: int,
        has_parallel: bool = False,
        has_loops: bool = False,
        estimated_iterations: int = 1,
    ) -> CostEstimate:
        """
        Estimate cost for a workflow execution.
        
        Args:
            node_count: Number of nodes in workflow
            has_parallel: Whether workflow has parallel branches
            has_loops: Whether workflow has loops
            estimated_iterations: Expected loop iterations
            
        Returns:
            CostEstimate with breakdown
        """
        try:
            costs = get_workflow_costs()
        except:
            costs = {
                "start": 1000,
                "node": 300,
                "parallel": 400,
                "loop": 200,
            }
        
        start_cost = costs.get("start", 1000)
        node_cost = costs.get("node", 300)
        parallel_cost = costs.get("parallel", 400) if has_parallel else 0
        loop_cost = costs.get("loop", 200) if has_loops else 0
        
        # Calculate node costs with iterations
        total_node_cost = node_cost * node_count * estimated_iterations
        
        estimated_cost = start_cost + total_node_cost + parallel_cost + loop_cost
        
        # Workflows are fairly predictable
        min_cost = start_cost + (node_cost * node_count)  # Single iteration
        max_cost = start_cost + (node_cost * node_count * max(estimated_iterations * 2, 3)) + parallel_cost + loop_cost
        
        return CostEstimate(
            estimated_cost=estimated_cost,
            min_cost=min_cost,
            max_cost=max_cost,
            confidence=ConfidenceLevel.HIGH.value,
            operation_type=OperationType.WORKFLOW.value,
            breakdown={
                "start_cost": start_cost,
                "node_cost": node_cost,
                "node_count": node_count,
                "parallel_cost": parallel_cost,
                "loop_cost": loop_cost,
                "estimated_iterations": estimated_iterations,
            },
            notes=[
                "Workflow costs are predictable based on structure",
                f"Estimate assumes {estimated_iterations} iteration(s)",
                "Loops may increase actual cost",
            ],
        )
    
    def estimate_code_execution(
        self,
        estimated_seconds: int = 10,
        memory_mb: int = 256,
    ) -> CostEstimate:
        """
        Estimate cost for code execution.
        
        Args:
            estimated_seconds: Expected execution time
            memory_mb: Memory allocation in MB
            
        Returns:
            CostEstimate with breakdown
        """
        try:
            costs = get_compute_costs()
        except:
            costs = {
                "second": 1,
                "execution_base": 5,
            }
        
        per_second = costs.get("second", 1)
        base_cost = costs.get("execution_base", 5)
        
        # Memory multiplier (base is 256MB)
        memory_multiplier = max(1.0, memory_mb / 256)
        
        compute_cost = per_second * estimated_seconds * memory_multiplier
        estimated_cost = int(base_cost + compute_cost)
        
        # Execution time can vary
        min_cost = base_cost + int(per_second * max(1, estimated_seconds // 2))
        max_cost = base_cost + int(per_second * estimated_seconds * 3 * memory_multiplier)
        
        return CostEstimate(
            estimated_cost=estimated_cost,
            min_cost=min_cost,
            max_cost=max_cost,
            confidence=ConfidenceLevel.MEDIUM.value,
            operation_type=OperationType.CODE_EXECUTION.value,
            breakdown={
                "base_cost": base_cost,
                "per_second": per_second,
                "estimated_seconds": estimated_seconds,
                "memory_mb": memory_mb,
                "memory_multiplier": memory_multiplier,
            },
            notes=[
                f"Estimate assumes {estimated_seconds} seconds execution",
                "Actual time depends on code complexity",
                f"Memory: {memory_mb}MB",
            ],
        )
    
    def estimate_batch(
        self,
        operations: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Estimate cost for multiple operations.
        
        Args:
            operations: List of operation dicts with type and params
            
        Returns:
            Combined estimate with individual breakdowns
        """
        estimates = []
        total_estimated = 0
        total_min = 0
        total_max = 0
        
        for op in operations:
            op_type = op.get("type", "chat")
            
            if op_type == "chat":
                est = self.estimate_chat(
                    message_length=op.get("message_length", 100),
                    model=op.get("model", "gpt-4o"),
                    provider=op.get("provider", "openai"),
                )
            elif op_type == "agent":
                est = self.estimate_agent_run(
                    agent_type=op.get("agent_type", "general"),
                    estimated_steps=op.get("estimated_steps", 5),
                )
            elif op_type == "workflow":
                est = self.estimate_workflow(
                    node_count=op.get("node_count", 5),
                    has_parallel=op.get("has_parallel", False),
                )
            elif op_type == "code_execution":
                est = self.estimate_code_execution(
                    estimated_seconds=op.get("estimated_seconds", 10),
                )
            else:
                continue
            
            estimates.append(est.to_dict())
            total_estimated += est.estimated_cost
            total_min += est.min_cost
            total_max += est.max_cost
        
        return {
            "total_estimated": total_estimated,
            "total_min": total_min,
            "total_max": total_max,
            "operation_count": len(estimates),
            "estimates": estimates,
        }


# Global instance
cost_estimator = CostEstimator()


# ============================================
# CONVENIENCE FUNCTIONS
# ============================================

def estimate_chat_cost(
    message_length: int,
    model: str = "gpt-4o",
    provider: str = "openai",
) -> Dict[str, Any]:
    """Estimate chat message cost."""
    return cost_estimator.estimate_chat(message_length, model, provider).to_dict()


def estimate_agent_cost(
    agent_type: str = "general",
    estimated_steps: int = 5,
) -> Dict[str, Any]:
    """Estimate agent run cost."""
    return cost_estimator.estimate_agent_run(agent_type, estimated_steps).to_dict()


def estimate_workflow_cost(
    node_count: int,
    has_parallel: bool = False,
) -> Dict[str, Any]:
    """Estimate workflow cost."""
    return cost_estimator.estimate_workflow(node_count, has_parallel).to_dict()
