"""Build the LangChain v1 agent: create_agent + middleware + Redis checkpointer."""
from __future__ import annotations

from langchain.agents import create_agent
from langchain.agents.middleware import (ModelCallLimitMiddleware, ModelFallbackMiddleware, ModelRetryMiddleware,
                                         SummarizationMiddleware, ToolCallLimitMiddleware)
from langchain_openrouter import ChatOpenRouter

from agent.config import get_agent_settings
from agent.middleware import CaseContextPrompt, ReplyPostCheck, TurnOutcome, WriteGuard
from agent.tools import TOOLS, AgentContext, CaseState


def make_model(name: str | None = None, **kw) -> ChatOpenRouter:
    s = get_agent_settings()
    # NB: langchain-openrouter's request_timeout is in MILLISECONDS (maps to the SDK's timeout_ms).
    return ChatOpenRouter(model=name or s.model, openrouter_api_key=s.openrouter_api_key, temperature=0,
                          max_retries=1, request_timeout=30_000, app_title="SetuHaul", **kw)


def build_agent(*, checkpointer=None, model=None, fallback_model=None, summarizer=None):
    s = get_agent_settings()
    model = model or make_model()
    middleware = [
        CaseContextPrompt(),                     # authoritative case state injected before every model call
        WriteGuard(),                            # no shipment tools before a load is linked; no hold on a LOW ETA
        ReplyPostCheck(),                        # no "booked" unless CONFIRMED; no dock a tool didn't return
        TurnOutcome(),                           # 2 failed clarifications -> coordinator; limits -> fixed reply
        ModelRetryMiddleware(max_retries=1),
        ToolCallLimitMiddleware(run_limit=10, exit_behavior="end"),
        ModelCallLimitMiddleware(run_limit=10, exit_behavior="end"),
        SummarizationMiddleware(summarizer or model, trigger=("messages", 40), keep=("messages", 16)),
    ]
    # fallback_model=False disables the fallback entirely (tests must never reach a real model)
    fb = None if fallback_model is False else (fallback_model or (make_model(s.fallback_model) if s.fallback_model else None))
    if fb is not None:
        middleware.append(ModelFallbackMiddleware(fb))
    return create_agent(model, TOOLS, middleware=middleware, state_schema=CaseState, context_schema=AgentContext,
                        checkpointer=checkpointer, name="setuhaul_driver_agent")
