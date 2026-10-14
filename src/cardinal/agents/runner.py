"""Run one agent call, record what it cost, and classify how it ended.

A `claude-code:` spec runs on the Claude Code subscription (agents/claude_code.py); every other spec,
and every call while CARDINAL_MODEL_PROVIDER is set, runs on Deep Agents.
"""

import logging
import os
import time
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from deepagents import create_deep_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import AIMessage
from langgraph.errors import GraphRecursionError
from pydantic import ValidationError

from cardinal.agents.backends import mounts
from cardinal.agents.provider import SUBSCRIPTION, resolve
from cardinal.graph.failures import FailureKind, StageFailure
from cardinal.store.db import now
from cardinal.store.recorder import Recorder

log = logging.getLogger(__name__)
SKILLS = Path(str(resources.files("cardinal").joinpath("skills")))
PROVIDER_MODULES = {"openai", "anthropic", "httpx", "httpcore", "google", "claude_agent_sdk"}


@dataclass
class Spend:
    """What a call used, kept up to date while it runs so a failed call is still recorded."""

    input_tokens: int = 0
    output_tokens: int = 0
    tools: list[str] = field(default_factory=list)
    transcript: list[dict] = field(default_factory=list)
    model: str | None = None  # the model that answered; claude-code only
    window: str | None = None  # the latest rate-limit window reported; claude-code only
    utilization: float | None = None


@dataclass
class AgentCall:
    stage: str
    model_spec: str
    prompt: str
    context: dict
    context_dir: Path
    worktree: Path | None = None
    repo_writable: bool = False
    off_limits: list[str] = field(default_factory=list)
    hidden: list[str] = field(default_factory=list)
    tools: list = field(default_factory=list)
    schema: type | None = None
    ticket_id: str | None = None


def usage(messages: list) -> tuple[int, int]:
    totals = [0, 0]
    for message in messages:
        if isinstance(message, AIMessage) and message.usage_metadata:
            totals[0] += message.usage_metadata.get("input_tokens", 0)
            totals[1] += message.usage_metadata.get("output_tokens", 0)
    return totals[0], totals[1]


def tool_names(messages: list) -> list[str]:
    return [call["name"] for message in messages if isinstance(message, AIMessage) for call in message.tool_calls]


def transcript(messages: list) -> list[dict]:
    return [{"type": message.type, "content": str(message.content)[:4000],
             "tool_calls": getattr(message, "tool_calls", None)} for message in messages]


def classify(exc: BaseException) -> FailureKind:
    if isinstance(exc, GraphRecursionError):
        return FailureKind.TURNS
    if type(exc).__module__.split(".")[0] in PROVIDER_MODULES:
        return FailureKind.PROVIDER
    return FailureKind.CRASH


def deep_agent(call: AgentCall, spend: Spend, recursion_limit: int, model_timeout: int):
    """Returns the raw structured response (or the final state when there is no schema)."""
    model = resolve(call.model_spec, {**call.context, "stage": call.stage}, model_timeout)
    agent = create_deep_agent(
        model=model,
        backend=mounts(call.worktree, call.context_dir, SKILLS, call.off_limits, call.hidden,
                       repo_writable=call.repo_writable),
        skills=["/skills/"],
        memory=["/skills/CONVENTIONS.md"],
        tools=call.tools,
        response_format=ToolStrategy(call.schema) if call.schema else None,
        # Never inherit the run's checkpointer: nothing resumes mid-agent, and a nested graph that
        # checkpoints every step deadlocks LangGraph's writer pool on small machines (2 vCPU CI).
        checkpointer=False,
    )
    # Stream values so the last state survives an exception and its spend is still recorded.
    state: dict = {}
    for state in agent.stream({"messages": [{"role": "user", "content": call.prompt}]},
                              config={"recursion_limit": recursion_limit}, stream_mode="values"):
        messages = state.get("messages", [])
        (spend.input_tokens, spend.output_tokens), spend.tools = usage(messages), tool_names(messages)
        spend.transcript = transcript(messages)
    return state.get("structured_response") if call.schema else state


def subscription(call: AgentCall, spend: Spend, recursion_limit: int, model_timeout: int):
    """Waits out a spent usage window and repeats the call: the work is slower, never failed or billed."""
    from cardinal.agents import claude_code  # the SDK is slow to import; only this path needs it

    while True:
        try:
            return claude_code.run(call, spend, recursion_limit, model_timeout, SKILLS)
        except claude_code.UsageLimit as exc:
            wait = max(exc.resets_at - time.time(), 0) + 60
            log.warning("%s hit the subscription usage limit; waiting %ss", call.stage, round(wait),
                        extra={"event": "usage_limit", "data": {"model": call.model_spec, "resets_at": exc.resets_at,
                                                                "detail": str(exc)}})
            time.sleep(wait)


def run_agent(call: AgentCall, recorder: Recorder, recursion_limit: int, model_timeout: int):
    """Returns the validated structured result (or the final state when there is no schema)."""
    engine = (subscription if call.model_spec.startswith(SUBSCRIPTION)
              and not os.environ.get("CARDINAL_MODEL_PROVIDER") else deep_agent)
    started, clock = now(), time.monotonic()
    spend = Spend()
    outcome = "ok"
    try:
        raw = engine(call, spend, recursion_limit, model_timeout)
        if call.schema is None:
            return raw
        if raw is None:
            raise StageFailure(FailureKind.NO_ANSWER, f"{call.stage} stopped without returning {call.schema.__name__}")
        try:
            return call.schema.model_validate(raw) if not isinstance(raw, call.schema) else raw
        except ValidationError as exc:
            raise StageFailure(FailureKind.INVALID_OUTPUT, f"{call.stage}: {exc}") from exc
    except StageFailure as exc:
        outcome = exc.kind.value
        raise
    except Exception as exc:
        kind = classify(exc)
        outcome = kind.value
        log.log(kind.level, "%s agent call failed: %s", call.stage, type(exc).__name__, exc_info=exc,
                extra={"event": "agent_call_failed", "failure_kind": kind.value, "data": {"model": call.model_spec}})
        raise StageFailure(kind, f"{call.stage}: {type(exc).__name__}: {str(exc)[:1500]}") from exc
    finally:
        tokens = (spend.input_tokens, spend.output_tokens)
        log.info("%s agent call ended %s", call.stage, outcome,
                 extra={"event": "agent_call", "data": {"model": call.model_spec, "ticket_id": call.ticket_id,
                                                         "seconds": round(time.monotonic() - clock, 2),
                                                         "tokens": tokens, "outcome": outcome}})
        recorder.agent_call(call.stage, call.ticket_id, started, time.monotonic() - clock,
                            tokens, spend.tools, outcome, spend.transcript,
                            model=spend.model, window=spend.window, utilization=spend.utilization)
