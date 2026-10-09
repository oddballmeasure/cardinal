-- Cardinal's record of what happened. Kept apart from checkpoints.db, which alone decides what a
-- paused run does next. Tables marked INPUT are read back to make decisions and must never be
-- written through the error-swallowing Recorder.

-- One attempt at one issue. A retry after an error is a new run with a new thread.
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL UNIQUE,
    repo TEXT NOT NULL,
    issue INTEGER NOT NULL,
    status TEXT NOT NULL,             -- running | awaiting_human | done | rejected | failed | triaged
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

-- INPUT. The claim lock: a row here means this process owns the issue until it settles. host and
-- pid name the owner, so the daemon can release a claim whose process was killed (store/db.py adds
-- them to older stores).
CREATE TABLE IF NOT EXISTS claims (
    repo TEXT NOT NULL,
    issue INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    host TEXT,
    pid INTEGER,
    PRIMARY KEY (repo, issue)
);

-- Every LogRecord Cardinal wrote or received, mirrored from the JSONL files under logs/.
-- Written through the swallowing log sink: a lost row loses a record, never a run.
CREATE TABLE IF NOT EXISTS logs (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    at TEXT NOT NULL,
    level TEXT NOT NULL,
    repo TEXT NOT NULL,
    component TEXT NOT NULL,
    event TEXT NOT NULL,
    failure_kind TEXT,
    fingerprint TEXT NOT NULL,
    run_id TEXT,
    record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS logs_by_fingerprint ON logs (fingerprint, at);
CREATE INDEX IF NOT EXISTS logs_by_at ON logs (at);

-- INPUT. How far the monitor has read the logs table.
CREATE TABLE IF NOT EXISTS monitor_cursor (
    name TEXT PRIMARY KEY,
    seq INTEGER NOT NULL
);

-- INPUT. One row per defect the monitor has filed; stops a second issue for the same fingerprint.
CREATE TABLE IF NOT EXISTS findings (
    fingerprint TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    issue INTEGER NOT NULL,
    occurrences INTEGER NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    filed_at TEXT NOT NULL
);

-- INPUT. Cardinal's judgement of all CI on each merge commit it made. A row stops a second re-run
-- or a second follow-up issue, across daemon restarts. `rerun` is the only verdict judged again.
CREATE TABLE IF NOT EXISTS post_merge (
    repo TEXT NOT NULL,
    merge_sha TEXT NOT NULL,
    run_id TEXT NOT NULL,
    verdict TEXT NOT NULL,             -- rerun | passed | no_checks | filing | filed | expired
    rerun_checks TEXT NOT NULL,        -- JSON ids of the failed check runs that were re-run
    follow_up INTEGER,                 -- the issue filed for checks that failed twice
    updated_at TEXT NOT NULL,
    PRIMARY KEY (repo, merge_sha)
);

-- INPUT. One `cardinal scout` pass over one repository. baseline_passed caches the test command's
-- result on base_sha, so repro tests are judged against a base known to be green.
CREATE TABLE IF NOT EXISTS scout_passes (
    pass_id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    autonomy TEXT NOT NULL,
    base_sha TEXT,
    baseline_passed INTEGER,
    counts TEXT,                       -- JSON: filed, dropped, held
    failure_kind TEXT,
    failure_detail TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT
);

-- INPUT. Every proposal the scout weighed, and what became of it. Filed rows are its track record:
-- outcome is read back from the issue each pass, and gates autonomy per category.
CREATE TABLE IF NOT EXISTS scout_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    pass_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    proposal TEXT NOT NULL,            -- JSON Proposal, so a held one can be weighed again
    status TEXT NOT NULL,              -- filed | dropped | held
    reason TEXT,                       -- why it was dropped or held, or why it filed as it did
    verdict TEXT,                      -- the reviewer's
    repro_ok INTEGER,
    filed_mode TEXT,                   -- proposed | auto | needs_human
    issue INTEGER,
    outcome TEXT,                      -- approved | rejected | done | error; NULL while undecided
    outcome_reason TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS scout_proposals_by_fingerprint ON scout_proposals (repo, fingerprint);

-- INPUT. When each survey area (its sorted paths) was last surveyed, so passes rotate through a repo.
CREATE TABLE IF NOT EXISTS scout_areas (
    repo TEXT NOT NULL,
    area TEXT NOT NULL,
    last_surveyed TEXT NOT NULL,
    PRIMARY KEY (repo, area)
);
