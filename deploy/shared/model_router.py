#!/usr/bin/env python3
"""
Open Manus — Model Router
Dynamic per-task LLM selection via OpenRouter.
Each agent has a default model, but can route specific task types
to specialized models for better cost/performance.
"""

import os
import json
from typing import Optional

# ============================================================
# Model Catalog — OpenRouter model IDs with cost info
# ============================================================
MODELS = {
    # General purpose / default
    "kimi-k2": "moonshotai/kimi-k2.5",
    
    # Code generation
    "glm-9b": "thudm/glm-4-9b-chat",
    "glm-z1-32b": "thudm/glm-z1-32b",
    "minimax-m2": "minimax/minimax-m2.5",
    
    # Web search optimized
    "qwen-3.5": "qwen/qwen3.5-397b-a17b",
    
    # Fast / cheap (background tasks, cron)
    "gemini-flash": "google/gemini-2.5-flash-preview",
    "step-flash": "stepfun/step-3.5-flash",
    
    # Multimodal
    "gemini-pro": "google/gemini-2.5-pro-preview",
    
    # Orchestration / finance
    "grok-4": "x-ai/grok-4.20-multi-agent-beta",
    
    # Escalation (expensive, use sparingly)
    "claude-opus": "anthropic/claude-opus-4-5",
    "gpt-5": "openai/gpt-5.2",
    
    # Tool use optimized
    "glm-5": "zhipu/glm-5",
    
    # Advanced knowledge
    "gpt-oss": "openai/gpt-oss-120b",
}

# ============================================================
# Task-Type → Model Routing Rules
# ============================================================
ROUTING_RULES = {
    # Task type → model key
    "default": "kimi-k2",
    "code_generation": "kimi-k2",
    "code_review": "minimax-m2",
    "software_engineering": "minimax-m2",
    "terminal_coding": "gpt-5",
    "web_search": "qwen-3.5",
    "research": "qwen-3.5",
    "deep_analysis": "gpt-oss",
    "knowledge_synthesis": "gpt-oss",
    "cron_job": "gemini-flash",
    "background_task": "gemini-flash",
    "quick_response": "step-flash",
    "multimodal": "gemini-pro",
    "image_analysis": "gemini-pro",
    "orchestration": "grok-4",
    "finance": "grok-4",
    "tool_use": "glm-5",
    "api_calls": "glm-5",
    "escalation": "claude-opus",
    "complex_reasoning": "claude-opus",
    "ad_copywriting": "kimi-k2",
    "social_media": "kimi-k2",
    "client_communication": "kimi-k2",
    "document_drafting": "kimi-k2",
    "data_analysis": "gemini-flash",
}


def get_model_for_task(task_type: str, agent_default: Optional[str] = None) -> str:
    """
    Get the optimal model ID for a given task type.
    Falls back to agent default, then global default.
    """
    if task_type in ROUTING_RULES:
        model_key = ROUTING_RULES[task_type]
        return MODELS.get(model_key, MODELS["kimi-k2"])
    
    if agent_default:
        return agent_default
    
    return MODELS["kimi-k2"]


def get_model_id(model_key: str) -> str:
    """Get the full OpenRouter model ID from a short key."""
    return MODELS.get(model_key, MODELS["kimi-k2"])


def list_models() -> dict:
    """Return all available models."""
    return MODELS.copy()


def list_routing_rules() -> dict:
    """Return all routing rules."""
    return ROUTING_RULES.copy()


if __name__ == "__main__":
    print("Open Manus — Model Router")
    print("=" * 50)
    print("\nAvailable Models:")
    for key, model_id in MODELS.items():
        print(f"  {key:20s} → {model_id}")
    print("\nRouting Rules:")
    for task, model_key in ROUTING_RULES.items():
        print(f"  {task:25s} → {model_key:15s} ({MODELS[model_key]})")
