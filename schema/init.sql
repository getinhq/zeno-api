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
CREATE TABLE users (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email       TEXT UNIQUE NOT NULL,
    name        TEXT,
    role        TEXT CHECK (role IN ('artist','supervisor','td','producer','admin','client')),
    department  TEXT,
    metadata    JSONB,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- 2. Projects
CREATE TABLE projects (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name        TEXT NOT NULL UNIQUE,
    code        TEXT NOT NULL UNIQUE,
    status      TEXT DEFAULT 'active'
                CHECK (status IN ('active', 'on_hold', 'archived')),
    start_date  DATE,
    end_date    DATE,
    metadata    JSONB,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TRIGGER projects_timestamp
    BEFORE UPDATE ON projects
    FOR EACH ROW EXECUTE PROCEDURE update_timestamp();

-- 3. Episodes
CREATE TABLE episodes (
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

CREATE TRIGGER episodes_timestamp
    BEFORE UPDATE ON episodes
    FOR EACH ROW EXECUTE PROCEDURE update_timestamp();

-- 4. Sequences
CREATE TABLE sequences (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    episode_id  UUID NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    code        TEXT NOT NULL,
    metadata    JSONB,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(episode_id, code)
);

CREATE TRIGGER sequences_timestamp
    BEFORE UPDATE ON sequences
    FOR EACH ROW EXECUTE PROCEDURE update_timestamp();

-- 5. Shots
CREATE TABLE shots (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    sequence_id     UUID NOT NULL REFERENCES sequences(id) ON DELETE CASCADE,
    shot_code       TEXT NOT NULL,
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

CREATE TRIGGER shots_timestamp
    BEFORE UPDATE ON shots
    FOR EACH ROW EXECUTE PROCEDURE update_timestamp();

-- 6. Assets
CREATE TABLE assets (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id  UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    type        TEXT NOT NULL
                CHECK (type IN ('character', 'prop', 'environment', 'fx', 'rig', 'texture_set', 'groom', 'shader')),
    name        TEXT NOT NULL,
    code        TEXT NOT NULL,
    metadata    JSONB,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(project_id, code)
);

CREATE TRIGGER assets_timestamp
    BEFORE UPDATE ON assets
    FOR EACH ROW EXECUTE PROCEDURE update_timestamp();

-- 7. Tasks
CREATE TABLE tasks (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    shot_id         UUID REFERENCES shots(id) ON DELETE CASCADE,
    asset_id        UUID REFERENCES assets(id),
    type            TEXT NOT NULL
                    CHECK (type IN ('model','rig','texture','layout','animation','fx','lighting','render','comp')),
    assignee_id     UUID REFERENCES users(id),
    status          TEXT DEFAULT 'todo'
                    CHECK (status IN ('todo','in_progress','review','done','blocked')),
    estimated_hours NUMERIC(6,2),
    actual_hours    NUMERIC(6,2),
    due_date        TIMESTAMPTZ,
    metadata        JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TRIGGER tasks_timestamp
    BEFORE UPDATE ON tasks
    FOR EACH ROW EXECUTE PROCEDURE update_timestamp();

-- 8. Versions (CAS: content_id; per-asset per-representation versioning)
CREATE TABLE versions (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    asset_id          UUID NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    representation    TEXT NOT NULL,
    version_number    INT NOT NULL,
    content_id        TEXT NOT NULL,
    filename          TEXT NOT NULL,
    task_id           UUID REFERENCES tasks(id) ON DELETE SET NULL,
    cid               TEXT,
    publish_batch_id  UUID,
    size_bytes        BIGINT,
    render_settings   JSONB,
    metadata          JSONB,
    published_by      UUID REFERENCES users(id),
    published_at      TIMESTAMPTZ DEFAULT NOW(),
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(asset_id, representation, version_number)
);

CREATE INDEX idx_versions_content_id ON versions(content_id);
CREATE INDEX idx_versions_asset_rep_num ON versions(asset_id, representation, version_number);
CREATE INDEX idx_versions_publish_batch ON versions(publish_batch_id);

-- 9. Shot–Asset casting
CREATE TABLE shot_assets (
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

CREATE INDEX idx_shot_assets_shot   ON shot_assets(shot_id);
CREATE INDEX idx_shot_assets_asset ON shot_assets(asset_id);

-- 10. Render jobs
CREATE TABLE render_jobs (
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

CREATE INDEX idx_render_jobs_status ON render_jobs(status);

-- 11. Workflows
CREATE TABLE workflows (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name        TEXT NOT NULL,
    steps       JSONB NOT NULL,
    trigger_on  TEXT,
    is_active   BOOLEAN DEFAULT true,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- 12. Reviews
CREATE TABLE reviews (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    version_id  UUID REFERENCES versions(id),
    reviewer_id UUID REFERENCES users(id),
    status      TEXT DEFAULT 'open'
                    CHECK (status IN ('open','approved','changes_requested')),
    notes       TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Extra indexes
CREATE INDEX idx_shots_sequence_status ON shots(sequence_id, status);
CREATE INDEX idx_tasks_assignee_status ON tasks(assignee_id, status);
CREATE INDEX idx_episodes_project_code ON episodes(project_id, code);

CREATE INDEX idx_assets_metadata_gin        ON assets USING GIN (metadata);
CREATE INDEX idx_versions_render_settings   ON versions USING GIN (render_settings);
CREATE INDEX idx_shot_assets_transform      ON shot_assets USING GIN (transform_data);
