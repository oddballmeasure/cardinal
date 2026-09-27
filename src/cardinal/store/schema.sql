-- Cardinal's record of what happened. Kept apart from checkpoints.db, which alone decides what a
-- paused run does next. Tables marked INPUT are read back to make decisions and must never be
-- written through the error-swallowing Recorder.

-- One attempt at one issue. A retry after an error is a new run with a new thread.
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL UNIQUE,
    repo TEXT NOT NULL,
    issue INTEGER NOT NULL,
    status TEXT NOT NULL,             -- running | awaiting_human | done | rejected | failed
    failure_kind TEXT,
    failure_detail TEXT,
    branch TEXT,
    head_sha TEXT,
    pr_number INTEGER,
    pr_url TEXT,
    merge_sha TEXT,
    deployed_sha TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT
);
CREATE INDEX IF NOT EXISTS runs_by_issue ON runs (repo, issue);

-- Everything a stage decided or observed, in order: decisions, tickets, verdicts, PR events.
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    at TEXT NOT NULL,
    stage TEXT NOT NULL,
    kind TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_run ON events (run_id, id);

-- One agent invocation: which role, how many tokens, which tools, how it ended.
CREATE TABLE IF NOT EXISTS agent_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    ticket_id TEXT,
    started_at TEXT NOT NULL,
    seconds REAL NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    tool_calls TEXT NOT NULL,
    outcome TEXT NOT NULL,
    transcript TEXT NOT NULL
);

-- INPUT. A profile costs model calls; losing it means paying again, so writes raise.
CREATE TABLE IF NOT EXISTS repo_profiles (
    repo TEXT PRIMARY KEY,
    revision TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    profile TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- INPUT. The claim lock: a row here means this process owns the issue until it settles.
CREATE TABLE IF NOT EXISTS claims (
    repo TEXT NOT NULL,
    issue INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    PRIMARY KEY (repo, issue)
);
