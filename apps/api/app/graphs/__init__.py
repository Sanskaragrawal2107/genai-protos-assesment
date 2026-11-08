"""LangGraph workflow definitions.

Each workflow is a small, single-purpose state machine composed of deterministic
Python nodes. LLM calls are isolated inside dedicated nodes, and external side
effects (Gmail send, Calendar write, Teams meeting) only happen inside nodes
that are reached after an approval decision.
"""
