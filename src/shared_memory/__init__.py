"""
Shared Memory MCP Server for Multi-Claude Coordination.

A centralized knowledge base and coordination system for multiple Claude instances
working across projects.
"""

from shared_memory.intent import get_current_context_tokens, get_current_intent_id

__version__ = "1.39.0"

__all__ = ["get_current_context_tokens", "get_current_intent_id"]
