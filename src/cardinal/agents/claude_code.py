"""Run one agent call on Claude Code, billed to the operator's subscription, never to an API key.

The model gets none of Claude Code's built-in tools. Its file tools are served in-process over MCP
and backed by the same mounts as the Deep Agents path (agents/backends.py), so `/repo/`, `/context/`
and `/skills/` keep their sandbox rules. Every call must authenticate with the subscription
(lessons/subscription-backend-never-bills.md): an API key in the environment is blanked, and a
session that still reports one is stopped before its first turn.
"""

import functools
import time
from contextlib import aclosing
from dataclasses import dataclass, field
from pathlib import Path

import anyio
from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKError, RateLimitEvent, ResultMessage,
                              SystemMessage, TextBlock, ToolAnnotations, ToolResultBlock, ToolUseBlock, UserMessage,
                              create_sdk_mcp_server, query, tool)
from deepagents.backends.utils import file_data_to_string, format_content_with_line_numbers, format_grep_matches

from cardinal.agents.backends import mounts
from cardinal.agents.provider import SUBSCRIPTION
from cardinal.agents.runner import Spend
from cardinal.graph.failures import FailureKind, StageFailure

SERVER = "cardinal"
# Either variable makes the CLI bill the API instead of the subscription; an empty value disables it.
BILLING_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
SUBSCRIPTION_SOURCE = "none"  # the init message's apiKeySource when the session runs on OAuth
RESULT_CHARS = 200_000  # above this Claude Code writes a result to a file, which this model cannot read
LIMIT_RETRY_SECONDS = 1800  # when a usage limit names no reset time


class UsageLimit(Exception):
    """The subscription's usage window is spent; the call can be repeated after `resets_at`."""

    def __init__(self, resets_at: float, detail: str) -> None:
        super().__init__(detail)
        self.resets_at = resets_at


@dataclass
class Watch:
    """Inactivity watchdog state. Time inside Cardinal's own tools (tests, CI waits) never counts."""

    last: float = field(default_factory=time.monotonic)
    busy: int = 0
    error: BaseException | None = None


def model_name(spec: str) -> str:
    return spec.removeprefix(SUBSCRIPTION)


def text(body: str, *, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": body}], "is_error": error}


def skill_index(skills: Path) -> str:
    """Name and description of each packaged skill, read from its frontmatter."""
    lines = []
    for path in sorted(skills.glob("*/SKILL.md")):
        meta = {}
        body = path.read_text().split("---")
        for line in (body[1] if len(body) > 2 else "").splitlines():
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
        lines.append(f"- {meta.get('name', path.parent.name)}: {meta.get('description', '')} "
                     f"(read /skills/{path.parent.name}/SKILL.md)")
    return "\n".join(lines)


def system_prompt(skills: Path) -> str:
    return ("You are an agent working through tools only. Paths are virtual: /repo/ is the repository, "
            "/context/ your stage's inputs and /skills/ your instructions. Before acting, read the SKILL.md "
            "of the skill your task names, in full.\n\n" + (skills / "CONVENTIONS.md").read_text()
            + "\n\n## Skills\n\n" + skill_index(skills))


def file_tools(backend, *, writable: bool) -> list:
    """The Deep Agents file tool surface, over Cardinal's own mounts."""
    big = ToolAnnotations(maxResultSizeChars=RESULT_CHARS)

    @tool("ls", "List one directory.", {"path": str}, annotations=big)
    async def ls(args: dict) -> dict:
        result = backend.ls(args["path"])
        if result.error:
            return text(result.error, error=True)
        return text("\n".join(entry["path"] + ("/" if entry.get("is_dir") else "") for entry in result.entries or []))

    @tool("read_file", "Read a file with line numbers. offset is the 0-based first line; limit the line count.",
          {"type": "object", "required": ["file_path"],
           "properties": {"file_path": {"type": "string"}, "offset": {"type": "integer", "default": 0},
                          "limit": {"type": "integer", "default": 2000}}}, annotations=big)
    async def read_file(args: dict) -> dict:
        result = backend.read(args["file_path"], args.get("offset", 0), args.get("limit", 2000))
        if result.error:
            return text(result.error, error=True)
        content = file_data_to_string(result.file_data) if result.file_data else ""
        return text(format_content_with_line_numbers(content, start_line=result.start_line or 1) or "(empty file)")

    @tool("glob", "Find files by glob pattern, e.g. '**/*.py', under path (default /).",
          {"type": "object", "required": ["pattern"],
           "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}}, annotations=big)
    async def glob(args: dict) -> dict:
        result = backend.glob(args["pattern"], args.get("path"))
        if result.error:
            return text(result.error, error=True)
        return text("\n".join(entry["path"] for entry in result.matches or []) or "No files found")

    @tool("grep", "Search file contents for a literal pattern, optionally under path and filtered by a glob.",
          {"type": "object", "required": ["pattern"],
           "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}, "glob": {"type": "string"}}},
          annotations=big)
    async def grep(args: dict) -> dict:
        result = backend.grep(args["pattern"], args.get("path"), args.get("glob"))
        if result.error:
            return text(result.error, error=True)
        body = format_grep_matches(result.matches or [], "content")
        return text(body + ("\n(results truncated)" if result.truncated else ""))

    @tool("write_file", "Create or overwrite a file.", {"file_path": str, "content": str})
    async def write_file(args: dict) -> dict:
        result = backend.write(args["file_path"], args["content"])
        return text(result.error, error=True) if result.error else text(f"Wrote {args['file_path']}")

    @tool("edit_file", "Replace old_string with new_string in a file; old_string must be unique unless replace_all.",
          {"type": "object", "required": ["file_path", "old_string", "new_string"],
           "properties": {"file_path": {"type": "string"}, "old_string": {"type": "string"},
                          "new_string": {"type": "string"}, "replace_all": {"type": "boolean", "default": False}}})
    async def edit_file(args: dict) -> dict:
        result = backend.edit(args["file_path"], args["old_string"], args["new_string"], args.get("replace_all", False))
        if result.error:
            return text(result.error, error=True)
        return text(f"Edited {args['file_path']} ({result.occurrences} replacement(s))")

    return [ls, read_file, glob, grep] + ([write_file, edit_file] if writable else [])


def role_tool(base, watch: Watch, cancel: anyio.CancelScope):
    """A role's LangChain tool, served over MCP. Its exceptions end the call, as they do on the other path."""
    schema = base.tool_call_schema.model_json_schema()
    schema.pop("title", None)

    @tool(base.name, base.description, schema, annotations=ToolAnnotations(maxResultSizeChars=RESULT_CHARS))
    async def invoke(args: dict) -> dict:
        watch.busy += 1
        try:
            return text(str(await anyio.to_thread.run_sync(base.invoke, args)))
        except Exception as exc:  # noqa: BLE001 - re-raised by run() once the session is stopped
            watch.error = exc
            cancel.cancel()
            return text(f"{type(exc).__name__}: {exc}", error=True)
        finally:
            watch.busy -= 1
            watch.last = time.monotonic()

    return invoke


def record(message, spend: Spend) -> None:
    if isinstance(message, AssistantMessage):
        calls = [{"name": block.name.removeprefix(f"mcp__{SERVER}__"), "args": block.input, "id": block.id}
                 for block in message.content if isinstance(block, ToolUseBlock)]
        spend.tools.extend(call["name"] for call in calls)
        words = "".join(block.text for block in message.content if isinstance(block, TextBlock))
        spend.transcript.append({"type": "ai", "content": words[:4000], "tool_calls": calls})
    elif isinstance(message, ResultMessage) and message.usage:
        # The session's totals; prompt caching moves most input into the cache counters.
        spend.input_tokens = sum(message.usage.get(key) or 0 for key in
                                 ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        spend.output_tokens = message.usage.get("output_tokens") or 0
    elif isinstance(message, UserMessage) and isinstance(message.content, list):
        for block in message.content:
            if isinstance(block, ToolResultBlock):
                spend.transcript.append({"type": "tool", "content": str(block.content)[:4000], "tool_calls": None})


def options(call, model: str, tools: list, max_turns: int, skills: Path) -> ClaudeAgentOptions:
    server = create_sdk_mcp_server(SERVER, tools=tools)
    return ClaudeAgentOptions(
        model=model, system_prompt=system_prompt(skills), cwd=str(call.context_dir),
        tools=[], allowed_tools=[f"mcp__{SERVER}__{item.name}" for item in tools],
        mcp_servers={SERVER: server}, strict_mcp_config=True, setting_sources=[], max_turns=max_turns,
        output_format={"type": "json_schema", "schema": call.schema.model_json_schema()} if call.schema else None,
        env={**dict.fromkeys(BILLING_ENV, ""), "MCP_TOOL_TIMEOUT": str(6 * 3600 * 1000)},
    )


async def session(call, model: str, max_turns: int, timeout: int, skills: Path, spend: Spend):
    watch = Watch()
    result: ResultMessage | None = None
    async with anyio.create_task_group() as group:
        backend = mounts(call.worktree, call.context_dir, skills, call.off_limits, call.hidden,
                         repo_writable=call.repo_writable)
        tools = (file_tools(backend, writable=call.repo_writable and call.worktree is not None)
                 + [role_tool(item, watch, group.cancel_scope) for item in call.tools])

        async def watchdog() -> None:
            while True:
                await anyio.sleep(5)
                if not watch.busy and time.monotonic() - watch.last > timeout:
                    watch.error = StageFailure(FailureKind.PROVIDER, f"{call.stage}: no response for {timeout}s")
                    group.cancel_scope.cancel()

        group.start_soon(watchdog)
        stream = query(prompt=call.prompt, options=options(call, model, tools, max_turns, skills))
        async with aclosing(stream):
            async for message in stream:
                watch.last = time.monotonic()
                record(message, spend)
                if isinstance(message, ResultMessage):
                    result = message
                # Kept, not raised: an exception inside the task group would leave it as an ExceptionGroup.
                watch.error = stop(message, call.stage)
                if watch.error is not None:
                    break
        group.cancel_scope.cancel()
    if watch.error is not None:
        raise watch.error
    return result


def stop(message, stage: str) -> Exception | None:
    """Why the session must end now: it would bill the API, or the subscription's window is spent."""
    if isinstance(message, SystemMessage) and message.subtype == "init":
        source = message.data.get("apiKeySource")
        if source != SUBSCRIPTION_SOURCE:
            return StageFailure(FailureKind.PROVIDER, f"{stage}: Claude Code reports API key source {source!r}; "
                                "refusing to bill the API")
    if isinstance(message, RateLimitEvent):
        info = message.rate_limit_info
        # Overage is pay-as-you-go: treat it as the end of the window, never as permission to continue.
        if info.status == "rejected" or info.raw.get("isUsingOverage"):
            return UsageLimit(info.resets_at or time.time() + LIMIT_RETRY_SECONDS,
                              f"{stage}: {info.rate_limit_type} usage limit {'overage' if info.raw.get('isUsingOverage') else info.status}")
    return None


def run(call, spend: Spend, recursion_limit: int, timeout: int, skills: Path):
    """Returns the raw structured output (or the final text when there is no schema)."""
    # LangGraph counts about two steps per tool turn, so the same limit bounds both engines alike.
    result = anyio.run(session, call, model_name(call.model_spec), max(recursion_limit // 2, 10), timeout, skills, spend)
    if result is None:
        raise StageFailure(FailureKind.PROVIDER, f"{call.stage}: Claude Code ended without a result")
    if result.subtype == "error_max_turns":
        raise StageFailure(FailureKind.TURNS, f"{call.stage} used {result.num_turns} turns without answering")
    if result.is_error:
        if result.api_error_status == 429:
            raise UsageLimit(time.time() + LIMIT_RETRY_SECONDS, f"{call.stage}: {result.result}")
        raise StageFailure(FailureKind.PROVIDER, f"{call.stage}: {result.subtype}: "
                           f"{(result.result or '; '.join(result.errors or []))[:1500]}")
    return result.structured_output if call.schema else result.result


async def ping(model: str) -> str:
    seen = {}
    opts = ClaudeAgentOptions(model=model, system_prompt="Reply with the single word ok.", tools=[],
                              strict_mcp_config=True, setting_sources=[], max_turns=1,
                              env=dict.fromkeys(BILLING_ENV, ""))
    async with aclosing(query(prompt="ok?", options=opts)) as stream:
        async for message in stream:
            reason = stop(message, "probe")
            if reason is not None:
                raise ValueError(str(reason))
            if isinstance(message, SystemMessage) and message.subtype == "init":
                seen["model"] = message.data.get("model")
            if isinstance(message, RateLimitEvent):
                seen["window"] = message.rate_limit_info.raw.get("unifiedWindows", {})
            if isinstance(message, ResultMessage) and message.is_error:
                raise ValueError(f"Claude Code probe failed: {message.result}")
    used = ", ".join(f"{name} {round(item.get('utilization', 0) * 100)}%" for name, item in seen.get("window", {}).items())
    return f"subscription {seen.get('model')}" + (f" ({used} used)" if used else "")


@functools.cache
def probe(spec: str) -> str:
    """One one-turn call per model proving it answers on the subscription, never on an API key."""
    try:
        return anyio.run(ping, model_name(spec))
    except ClaudeSDKError as exc:
        raise ValueError(f"Claude Code is unavailable: {exc}") from exc
