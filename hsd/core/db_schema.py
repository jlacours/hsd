"""SQLite schema definitions for the task database."""

TASKS_TABLE_BODY = """
    id              INTEGER PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    destination     TEXT NOT NULL,
    owner_harness   TEXT,
    owner_model     TEXT,
    stage           TEXT NOT NULL CHECK (stage IN
        ('todo','in-progress','done','reviewed','to-be-revised-by-human','closed')),
    status          TEXT NOT NULL CHECK (status IN
        ('queued','in-progress','blocked','complete')),
    source_harness  TEXT NOT NULL,
    source_model    TEXT NOT NULL,
    model_check_note TEXT,
    author          TEXT,
    working_dir     TEXT,
    repository      TEXT,
    branch_commit   TEXT,
    tree_state      TEXT,
    diff            TEXT,
    verify_cmd      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
"""

TASKS_COLUMNS = (
    "id, slug, title, destination, owner_harness, owner_model, stage, status, "
    "source_harness, source_model, model_check_note, author, working_dir, "
    "repository, branch_commit, tree_state, diff, verify_cmd, "
    "created_at, updated_at"
)

SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS tasks ({TASKS_TABLE_BODY});

CREATE TABLE IF NOT EXISTS sections (
    task_id   INTEGER NOT NULL REFERENCES tasks(id),
    name      TEXT NOT NULL CHECK (name IN
        ('objective','current_state','summary_for_review','work_completed',
         'files_changed','commands_verification','decisions_assumptions',
         'blockers_risks','warnings','next_actions','artifacts',
         'continuation_prompt','raw')),
    content   TEXT NOT NULL,
    PRIMARY KEY (task_id, name)
);

CREATE TABLE IF NOT EXISTS transitions (
    id            INTEGER PRIMARY KEY,
    task_id       INTEGER NOT NULL REFERENCES tasks(id),
    at            TEXT NOT NULL,
    actor_harness TEXT NOT NULL,
    actor_model   TEXT NOT NULL,
    from_stage    TEXT,
    to_stage      TEXT NOT NULL,
    note          TEXT
);

CREATE TABLE IF NOT EXISTS reviews (
    id                INTEGER PRIMARY KEY,
    task_id           INTEGER NOT NULL REFERENCES tasks(id),
    at                TEXT NOT NULL,
    reviewer_harness  TEXT NOT NULL,
    reviewer_model    TEXT NOT NULL,
    verdict           TEXT NOT NULL CHECK (verdict IN
        ('accepted','changes-requested','human-revision-required')),
    findings          TEXT NOT NULL,
    disposition       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_stage ON tasks(stage);
CREATE INDEX IF NOT EXISTS idx_tasks_dest  ON tasks(destination, stage);
CREATE INDEX IF NOT EXISTS idx_trans_task  ON transitions(task_id, at);
"""
