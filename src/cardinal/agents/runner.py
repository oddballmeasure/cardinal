"""Run one Deep Agents call, record what it cost, and classify how it ended."""

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
from cardinal.agents.provider import resolve
from cardinal.graph.failures import FailureKind, StageFailure
from cardinal.store.db import now
from cardinal.store.recorder import Recorder

SKILLS = Path(str(resources.files("cardinal").joinpath("skills")))
PROVIDER_MODULES = {"openai", "anthropic", "httpx", "httpcore", "google"}


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


def run_agent(call: AgentCall, recorder: Recorder, recursion_limit: int, model_timeout: int):
    """Returns the validated structured result (or the final state when there is no schema)."""
    model = resolve(call.model_spec, {**call.context, "stage": call.stage}, model_timeout)
    agent = create_deep_agent(
        model=model,
        backend=mounts(call.worktree, call.context_dir, SKILLS, call.off_limits, repo_writable=call.repo_writable),
        skills=["/skills/"],
        memory=["/skills/CONVENTIONS.md"],
        tools=call.tools,
        response_format=ToolStrategy(call.schema) if call.schema else None,
    )
    started, clock = now(), time.monotonic()
    state: dict = {"messages": []}
    outcome = "ok"
    try:
        # Stream values so the last state survives an exception and its spend is still recorded.
        for state in agent.stream({"messages": [{"role": "user", "content": call.prompt}]},
                                  config={"recursion_limit": recursion_limit}, stream_mode="values"):
            pass
        if call.schema is None:
            return state
        raw = state.get("structured_response")
        if raw is None:
            outcome = FailureKind.NO_ANSWER.value
            raise StageFailure(FailureKind.NO_ANSWER, f"{call.stage} stopped without returning {call.schema.__name__}")
        try:
            return call.schema.model_validate(raw) if not isinstance(raw, call.schema) else raw
        except ValidationError as exc:
            outcome = FailureKind.INVALID_OUTPUT.value
            raise StageFailure(FailureKind.INVALID_OUTPUT, f"{call.stage}: {exc}") from exc
    except StageFailure:
        raise
    except Exception as exc:
        kind = classify(exc)
        outcome = kind.value
        raise StageFailure(kind, f"{call.stage}: {type(exc).__name__}: {str(exc)[:1500]}") from exc
    finally:
        messages = state.get("messages", [])
        recorder.agent_call(call.stage, call.ticket_id, started, time.monotonic() - clock,
                            usage(messages), tool_names(messages), outcome, transcript(messages))
