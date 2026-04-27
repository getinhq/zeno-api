-- =====================================================================
-- Zeno / CG Pipeline – PostgreSQL schema (CAS-native, representation-aware)
-- Settings: in MongoDB (settings_global, settings_project). Postgres = published data only.
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE OR REPLACE FUNCTION update_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 1. Users
CREATE TABLE IF NOT EXISTS users (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email         TEXT UNIQUE NOT NULL,
    username      TEXT UNIQUE,
    password_hash TEXT,
    name          TEXT,
    role          TEXT CHECK (role IN ('artist','supervisor','td','producer','admin','client')),
    app_role      TEXT CHECK (app_role IN ('artist','pipeline','supervisor','production')),
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    department    TEXT,
    metadata      JSONB,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Backward-compatible migration for existing DBs created before auth fields.
ALTER TABLE users ADD COLUMN IF NOT EXISTS username      TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS app_role      TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active     BOOLEAN NOT NULL DEFAULT TRUE;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'users_app_role_check'
    ) THEN
        ALTER TABLE users
        ADD CONSTRAINT users_app_role_check
        CHECK (app_role IS NULL OR app_role IN ('artist','pipeline','supervisor','production'));
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes WHERE indexname = 'users_username_key'
    ) THEN
        CREATE UNIQUE INDEX users_username_key ON users(username) WHERE username IS NOT NULL;
    END IF;
END $$;

-- Seed app_role from legacy role column when still empty.
UPDATE users
   SET app_role = CASE role
                    WHEN 'td'       THEN 'pipeline'
                    WHEN 'producer' THEN 'production'
                    WHEN 'admin'    THEN 'pipeline'
                    WHEN 'client'   THEN 'production'
                    WHEN 'artist'     THEN 'artist'
                    WHEN 'supervisor' THEN 'supervisor'
                    ELSE NULL
                  END
 WHERE app_role IS NULL AND role IS NOT NULL;

-- 2. Projects
CREATE TABLE IF NOT EXISTS projects (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name        TEXT NOT NULL UNIQUE,
    code        TEXT NOT NULL UNIQUE,
    status      TEXT DEFAULT 'active'
                CHECK (status IN ('active', 'on_hold', 'archived', 'completed', 'approved')),
    start_date  DATE,
    end_date    DATE,
    metadata    JSONB,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

DROP TRIGGER IF EXISTS projects_timestamp ON projects;
CREATE TRIGGER projects_timestamp
    BEFORE UPDATE ON projects
    FOR EACH ROW EXECUTE PROCEDURE update_timestamp();

-- Backward-compatible migration: widen status CHECK to include 'completed' and 'approved'.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'projects_status_check'
    ) THEN
        ALTER TABLE projects DROP CONSTRAINT projects_status_check;
    END IF;
    ALTER TABLE projects
    ADD CONSTRAINT projects_status_check
    CHECK (status IN ('active', 'on_hold', 'archived', 'completed', 'approved'));
END $$;

-- 3. Episodes
CREATE TABLE IF NOT EXISTS episodes (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id      UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    episode_number  INT NOT NULL,
    title           TEXT,
    code            TEXT NOT NULL,
    status          TEXT DEFAULT 'in_production',
    air_date        DATE,
    metadata        JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(project_id, code)
);

DROP TRIGGER IF EXISTS episodes_timestamp ON episodes;
CREATE TRIGGER episodes_timestamp
    BEFORE UPDATE ON episodes
    FOR EACH ROW EXECUTE PROCEDURE update_timestamp();

-- 4. Sequences
CREATE TABLE IF NOT EXISTS sequences (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    episode_id  UUID NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    code        TEXT NOT NULL,
    stage       TEXT NOT NULL DEFAULT 'Layout'
                CHECK (stage IN ('Animatics', 'Layout', 'Animation', 'Lighting', 'Comp')),
    start_frame INT,
    end_frame   INT,
    metadata    JSONB,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(episode_id, code)
);

DROP TRIGGER IF EXISTS sequences_timestamp ON sequences;
CREATE TRIGGER sequences_timestamp
    BEFORE UPDATE ON sequences
    FOR EACH ROW EXECUTE PROCEDURE update_timestamp();

-- Backward-compatible migration for existing DBs created before stage field.
ALTER TABLE sequences ADD COLUMN IF NOT EXISTS stage TEXT;
UPDATE sequences SET stage = 'Layout' WHERE stage IS NULL;
ALTER TABLE sequences ALTER COLUMN stage SET DEFAULT 'Layout';
ALTER TABLE sequences ALTER COLUMN stage SET NOT NULL;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'sequences_stage_check'
    ) THEN
        ALTER TABLE sequences
        ADD CONSTRAINT sequences_stage_check
        CHECK (stage IN ('Animatics', 'Layout', 'Animation', 'Lighting', 'Comp'));
    END IF;
END $$;

ALTER TABLE sequences ADD COLUMN IF NOT EXISTS start_frame INT;
ALTER TABLE sequences ADD COLUMN IF NOT EXISTS end_frame INT;

-- 5. Shots
CREATE TABLE IF NOT EXISTS shots (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    sequence_id     UUID NOT NULL REFERENCES sequences(id) ON DELETE CASCADE,
    shot_code       TEXT NOT NULL,
    stage           TEXT NOT NULL DEFAULT 'Layout'
                    CHECK (stage IN ('Animatics', 'Layout', 'Animation', 'Lighting', 'Comp')),
    frame_start     INT,
    frame_end       INT,
    handle_in       INT DEFAULT 0,
    handle_out      INT DEFAULT 0,
    status          TEXT DEFAULT 'pending'
                    CHECK (status IN ('pending', 'in_progress', 'review', 'approved', 'final')),
    metadata        JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(sequence_id, shot_code)
);

DROP TRIGGER IF EXISTS shots_timestamp ON shots;
CREATE TRIGGER shots_timestamp
    BEFORE UPDATE ON shots
    FOR EACH ROW EXECUTE PROCEDURE update_timestamp();

-- Backward-compatible migration for existing DBs created before stage field.
ALTER TABLE shots ADD COLUMN IF NOT EXISTS stage TEXT;
UPDATE shots SET stage = 'Layout' WHERE stage IS NULL;
ALTER TABLE shots ALTER COLUMN stage SET DEFAULT 'Layout';
ALTER TABLE shots ALTER COLUMN stage SET NOT NULL;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'shots_stage_check'
    ) THEN
        ALTER TABLE shots
        ADD CONSTRAINT shots_stage_check
        CHECK (stage IN ('Animatics', 'Layout', 'Animation', 'Lighting', 'Comp'));
    END IF;
END $$;

-- 6. Assets
CREATE TABLE IF NOT EXISTS assets (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id  UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    type        TEXT NOT NULL
                CHECK (type IN ('character', 'prop', 'environment', 'fx', 'rig', 'texture_set', 'groom', 'shader')),
    name        TEXT NOT NULL,
    code        TEXT NOT NULL,
    pipeline_stages TEXT[] DEFAULT '{}',
    pipeline_stage_status JSONB DEFAULT '{}'::jsonb,
    metadata    JSONB,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(project_id, code)
);

DROP TRIGGER IF EXISTS assets_timestamp ON assets;
CREATE TRIGGER assets_timestamp
    BEFORE UPDATE ON assets
    FOR EACH ROW EXECUTE PROCEDURE update_timestamp();

ALTER TABLE assets ADD COLUMN IF NOT EXISTS pipeline_stages TEXT[] DEFAULT '{}';
ALTER TABLE assets ADD COLUMN IF NOT EXISTS pipeline_stage_status JSONB DEFAULT '{}'::jsonb;

-- 7. Tasks
CREATE TABLE IF NOT EXISTS tasks (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id      UUID REFERENCES projects(id) ON DELETE CASCADE,
    shot_id         UUID REFERENCES shots(id) ON DELETE CASCADE,
    asset_id        UUID REFERENCES assets(id),
    type            TEXT NOT NULL
                    CHECK (type IN ('model','rig','texture','layout','animation','fx','lighting','render','comp')),
    title           TEXT,
    description     TEXT,
    assignee_id     UUID REFERENCES users(id),
    reviewer_id     UUID REFERENCES users(id),
    status          TEXT DEFAULT 'todo'
                    CHECK (status IN ('todo','in_progress','review','done','blocked')),
    estimated_hours NUMERIC(6,2),
    actual_hours    NUMERIC(6,2),
    due_date        TIMESTAMPTZ,
    metadata        JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

DROP TRIGGER IF EXISTS tasks_timestamp ON tasks;
CREATE TRIGGER tasks_timestamp
    BEFORE UPDATE ON tasks
    FOR EACH ROW EXECUTE PROCEDURE update_timestamp();

-- Backward-compatible migration for existing DBs created before the new fields.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS project_id  UUID REFERENCES projects(id) ON DELETE CASCADE;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS title       TEXT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS description TEXT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS reviewer_id UUID REFERENCES users(id);

-- Backfill project_id from shot or asset join when possible, so filters work on old rows.
UPDATE tasks t
   SET project_id = COALESCE(
        (SELECT p.id FROM shots s
           JOIN sequences seq ON s.sequence_id = seq.id
           JOIN episodes e   ON seq.episode_id = e.id
           JOIN projects p   ON e.project_id  = p.id
          WHERE s.id = t.shot_id),
        (SELECT p.id FROM assets a
           JOIN projects p ON a.project_id = p.id
          WHERE a.id = t.asset_id)
   )
 WHERE t.project_id IS NULL
   AND (t.shot_id IS NOT NULL OR t.asset_id IS NOT NULL);

CREATE INDEX IF NOT EXISTS idx_tasks_project_status ON tasks(project_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_reviewer       ON tasks(reviewer_id);

-- Task ↔ users (many assignees, many collaborators).
CREATE TABLE IF NOT EXISTS task_assignees (
    task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_task_assignees_user ON task_assignees(user_id);

CREATE TABLE IF NOT EXISTS task_collaborators (
    task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_task_collaborators_user ON task_collaborators(user_id);

-- 8. Versions (CAS: content_id; per-asset per-representation per-pipeline-stage versioning)
CREATE TABLE IF NOT EXISTS versions (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    asset_id          UUID NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    representation    TEXT NOT NULL,
    pipeline_stage    TEXT NOT NULL DEFAULT '',
    version_number    INT NOT NULL,
    content_id        TEXT NOT NULL,
    filename          TEXT NOT NULL,
    task_id           UUID REFERENCES tasks(id) ON DELETE SET NULL,
    cid               TEXT,
    publish_batch_id  UUID,
    size_bytes        BIGINT,
    render_settings   JSONB,
    metadata          JSONB,
    feedback          TEXT,
    status            TEXT DEFAULT 'pending'
                    CHECK (status IN ('pending', 'in_review', 'approved', 'rejected')),
    published_by      UUID REFERENCES users(id),
    published_at      TIMESTAMPTZ DEFAULT NOW(),
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT versions_asset_rep_stage_ver UNIQUE (asset_id, representation, pipeline_stage, version_number)
);

ALTER TABLE versions ADD COLUMN IF NOT EXISTS pipeline_stage TEXT NOT NULL DEFAULT '';
ALTER TABLE versions ADD COLUMN IF NOT EXISTS feedback TEXT;
ALTER TABLE versions ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'pending';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'versions_status_check'
  ) THEN
    ALTER TABLE versions ADD CONSTRAINT versions_status_check
      CHECK (status IN ('pending', 'in_review', 'approved', 'rejected'));
  END IF;
END $$;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'versions_asset_id_representation_version_number_key'
  ) THEN
    ALTER TABLE versions DROP CONSTRAINT versions_asset_id_representation_version_number_key;
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'versions_asset_rep_stage_ver'
  ) THEN
    ALTER TABLE versions ADD CONSTRAINT versions_asset_rep_stage_ver
      UNIQUE (asset_id, representation, pipeline_stage, version_number);
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_versions_content_id ON versions(content_id);
CREATE INDEX IF NOT EXISTS idx_versions_asset_rep_num ON versions(asset_id, representation, version_number);
CREATE INDEX IF NOT EXISTS idx_versions_asset_stage_rep_num ON versions(asset_id, pipeline_stage, representation, version_number);
CREATE INDEX IF NOT EXISTS idx_versions_publish_batch ON versions(publish_batch_id);

-- 9. Shot–Asset casting
CREATE TABLE IF NOT EXISTS shot_assets (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    shot_id         UUID NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
    asset_id        UUID NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    version_id      UUID REFERENCES versions(id),
    instance_name   TEXT,
    role            TEXT,
    transform_data  JSONB,
    sort_order      INT DEFAULT 0,
    is_active       BOOLEAN DEFAULT true,
    created_by      UUID REFERENCES users(id),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(shot_id, asset_id, instance_name)
);

CREATE INDEX IF NOT EXISTS idx_shot_assets_shot   ON shot_assets(shot_id);
CREATE INDEX IF NOT EXISTS idx_shot_assets_asset ON shot_assets(asset_id);

-- 10. Render jobs
CREATE TABLE IF NOT EXISTS render_jobs (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    version_id      UUID REFERENCES versions(id),
    shot_id         UUID REFERENCES shots(id),
    status          TEXT DEFAULT 'queued'
                    CHECK (status IN ('queued','running','done','failed','cancelled')),
    priority        INT DEFAULT 50,
    farm_node       TEXT,
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    render_time_sec INT,
    output_cid      TEXT,
    settings        JSONB,
    error_log       TEXT
);

CREATE INDEX IF NOT EXISTS idx_render_jobs_status ON render_jobs(status);

-- 11. Workflows
CREATE TABLE IF NOT EXISTS workflows (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name        TEXT NOT NULL,
    steps       JSONB NOT NULL,
    trigger_on  TEXT,
    is_active   BOOLEAN DEFAULT true,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- 12. Reviews
CREATE TABLE IF NOT EXISTS reviews (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    version_id  UUID REFERENCES versions(id),
    reviewer_id UUID REFERENCES users(id),
    status      TEXT DEFAULT 'open'
                    CHECK (status IN ('open','approved','changes_requested')),
    notes       TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Extra indexes
CREATE INDEX IF NOT EXISTS idx_shots_sequence_status ON shots(sequence_id, status);
CREATE INDEX IF NOT EXISTS idx_shots_sequence_stage ON shots(sequence_id, stage);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee_status ON tasks(assignee_id, status);
CREATE INDEX IF NOT EXISTS idx_episodes_project_code ON episodes(project_id, code);
CREATE INDEX IF NOT EXISTS idx_sequences_episode_stage ON sequences(episode_id, stage);

CREATE INDEX IF NOT EXISTS idx_assets_metadata_gin        ON assets USING GIN (metadata);
CREATE INDEX IF NOT EXISTS idx_versions_render_settings   ON versions USING GIN (render_settings);
CREATE INDEX IF NOT EXISTS idx_shot_assets_transform      ON shot_assets USING GIN (transform_data);

-- 13. Project membership (for team-member events / audience targeting).
CREATE TABLE IF NOT EXISTS project_members (
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    added_at   TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (project_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_project_members_user ON project_members(user_id);

-- 14. Issues (Not-Started → In-Progress → Testing → Closed).
CREATE TABLE IF NOT EXISTS issues (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id   UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title        TEXT NOT NULL,
    body         TEXT,
    status       TEXT NOT NULL DEFAULT 'not_started'
                 CHECK (status IN ('not_started','in_progress','testing','closed')),
    reporter_id  UUID REFERENCES users(id),
    assignee_id  UUID REFERENCES users(id),
    asset_id     UUID REFERENCES assets(id) ON DELETE SET NULL,
    shot_id      UUID REFERENCES shots(id)  ON DELETE SET NULL,
    dcc          TEXT,
    metadata     JSONB,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    updated_at   TIMESTAMPTZ DEFAULT NOW()
);

DROP TRIGGER IF EXISTS issues_timestamp ON issues;
CREATE TRIGGER issues_timestamp
    BEFORE UPDATE ON issues
    FOR EACH ROW EXECUTE PROCEDURE update_timestamp();

-- Backward-compatible migration: rename legacy 'unassigned' → 'not_started',
-- widen the CHECK, add entity links. Drop the old CHECK first so the UPDATE
-- below is legal against DBs that still hold 'unassigned' rows.
ALTER TABLE issues ADD COLUMN IF NOT EXISTS asset_id UUID REFERENCES assets(id) ON DELETE SET NULL;
ALTER TABLE issues ADD COLUMN IF NOT EXISTS shot_id  UUID REFERENCES shots(id)  ON DELETE SET NULL;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'issues_status_check'
    ) THEN
        ALTER TABLE issues DROP CONSTRAINT issues_status_check;
    END IF;
END $$;

UPDATE issues SET status = 'not_started' WHERE status = 'unassigned';

ALTER TABLE issues
    ADD CONSTRAINT issues_status_check
    CHECK (status IN ('not_started','in_progress','testing','closed'));

ALTER TABLE issues ALTER COLUMN status SET DEFAULT 'not_started';

CREATE INDEX IF NOT EXISTS idx_issues_project_status ON issues(project_id, status);
CREATE INDEX IF NOT EXISTS idx_issues_reporter       ON issues(reporter_id);
CREATE INDEX IF NOT EXISTS idx_issues_assignee       ON issues(assignee_id);
CREATE INDEX IF NOT EXISTS idx_issues_asset          ON issues(asset_id);
CREATE INDEX IF NOT EXISTS idx_issues_shot           ON issues(shot_id);

-- 15. Issue attachments (media / screenshots stored in CAS).
CREATE TABLE IF NOT EXISTS issue_attachments (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    issue_id    UUID NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    content_id  TEXT NOT NULL,
    filename    TEXT NOT NULL,
    mime_type   TEXT,
    size_bytes  BIGINT,
    uploaded_by UUID REFERENCES users(id),
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_issue_attachments_issue ON issue_attachments(issue_id);

-- 16. Notifications (project-scoped bell dropdown).
--     user_id NULL means "project-wide" (Management roles see it).
CREATE TABLE IF NOT EXISTS notifications (
    id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id    UUID REFERENCES users(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    payload    JSONB NOT NULL DEFAULT '{}'::jsonb,
    read_at    TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_notifications_project_created ON notifications(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_user_unread     ON notifications(user_id, read_at);

-- 17. Events (activity feed).
CREATE TABLE IF NOT EXISTS events (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id  UUID REFERENCES projects(id) ON DELETE CASCADE,
    actor_id    UUID REFERENCES users(id) ON DELETE SET NULL,
    kind        TEXT NOT NULL,
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_events_project_created ON events(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_kind            ON events(kind);
