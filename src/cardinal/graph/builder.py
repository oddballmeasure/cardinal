"""profile → intake → [human ↺ intake] → implement ⇄ verify → publish → pr → [deploy] → done"""

from functools import partial

from langgraph.graph import END, START, StateGraph

from cardinal.graph import nodes
from cardinal.graph.context import Run
from cardinal.graph.state import RunState


def failed(state: RunState) -> bool:
    return state.get("status") == "failed"


def after_intake(state: RunState) -> str:
    status = state.get("status")
    if status == "awaiting_human":
        return "human"
    return "implement" if status == "running" else END


def after_human(state: RunState) -> str:
    return "intake" if state.get("status") == "running" else END


def after_verify(state: RunState) -> str:
    if failed(state):
        return END
    return "implement" if state.get("repair") else "publish"


def after_pr(run: Run, state: RunState) -> str:
    if failed(state):
        return END
    return "deploy" if run.repo.deploy else "done"


def on_success(target: str):
    return lambda state: END if failed(state) else target


def build(run: Run, checkpointer):
    graph = StateGraph(RunState)
    for name in ("profile", "intake", "human", "implement", "verify", "publish", "pr", "deploy", "done"):
        graph.add_node(name, partial(getattr(nodes, f"{name}_node"), run=run))
    graph.add_edge(START, "profile")
    graph.add_conditional_edges("profile", on_success("intake"))
    graph.add_conditional_edges("intake", after_intake)
    graph.add_conditional_edges("human", after_human)
    graph.add_conditional_edges("implement", on_success("verify"))
    graph.add_conditional_edges("verify", after_verify)
    graph.add_conditional_edges("publish", on_success("pr"))
    graph.add_conditional_edges("pr", partial(after_pr, run))
    graph.add_conditional_edges("deploy", on_success("done"))
    graph.add_edge("done", END)
    return graph.compile(checkpointer=checkpointer)
