An agent run inside a graph node inherits the run's checkpointer and can deadlock LangGraph's checkpoint writers on a 2-vCPU machine.

On 2026-10-02 the self-test repo's first CI run (GitHub's 2-vCPU runner) hung for over an hour in the
`single` scenario, which takes 20 seconds locally. A `py-spy` dump showed the profiler's Deep Agents
graph waiting on six `_checkpointer_put_after_previous` threads, each waiting on the write queued before it.
The pool has `min(32, cpus + 4)` workers, so on small machines the oldest write never got a thread.
`PYTHON_CPU_COUNT=2` reproduces it locally. `run_agent` now passes `checkpointer=False`: nothing
resumes mid-agent anyway. Run the offline suite with `PYTHON_CPU_COUNT=2` after changing how agents run.

The same run showed a second defect: `subprocess.run(timeout=...)` kills only the child, then waits
forever on pipes a grandchild holds, so the E2E test's 900s timeout never fired. The harness uses
`product.run_bounded`, which kills the whole process group.
