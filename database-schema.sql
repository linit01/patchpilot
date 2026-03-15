-- =============================================================================
-- PatchPilot — Canonical Database Schema
-- =============================================================================
-- This file is documentation/reference. The app auto-creates all tables on
-- startup. You do NOT need to run this manually unless bootstrapping manually.
-- =============================================================================

CREATE TABLE IF NOT EXISTS hosts (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    hostname          VARCHAR(255) UNIQUE NOT NULL,
    ip_address        VARCHAR(45),
    os_type           VARCHAR(50),
    os_family         VARCHAR(50),
    last_checked      TIMESTAMP WITH TIME ZONE,
    status            VARCHAR(50)  DEFAULT 'unknown',
    total_updates     INTEGER      DEFAULT 0,
    reboot_required   BOOLEAN      DEFAULT FALSE,
    allow_auto_reboot BOOLEAN      DEFAULT TRUE,
    ssh_user          VARCHAR(100) DEFAULT 'root',
    ssh_port          INTEGER      DEFAULT 22,
    ssh_key_type      VARCHAR(50)  DEFAULT 'default',
    ssh_private_key_encrypted  BYTEA,
    ssh_password_encrypted     BYTEA,
    notes             TEXT,
    tags              VARCHAR(255),
    is_control_node   BOOLEAN      DEFAULT FALSE,
    created_at        TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at        TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    created_by        UUID REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_hosts_hostname ON hosts(hostname);
CREATE INDEX IF NOT EXISTS idx_hosts_status   ON hosts(status);

CREATE TABLE IF NOT EXISTS packages (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    host_id           UUID REFERENCES hosts(id) ON DELETE CASCADE,
    package_name      VARCHAR(255) NOT NULL,
    current_version   VARCHAR(100),
    available_version VARCHAR(100),
    update_type       VARCHAR(50),
    detected_at       TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(host_id, package_name)
);
CREATE INDEX IF NOT EXISTS idx_packages_host_id ON packages(host_id);

CREATE TABLE IF NOT EXISTS patch_history (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    host_id          UUID REFERENCES hosts(id) ON DELETE CASCADE,
    execution_time   TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    packages_updated TEXT[],
    success          BOOLEAN,
    error_message    TEXT,
    duration_seconds INTEGER,
    output           TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_patch_history_host_id        ON patch_history(host_id);
CREATE INDEX IF NOT EXISTS idx_patch_history_execution_time ON patch_history(execution_time);

CREATE TABLE IF NOT EXISTS users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username      VARCHAR(50)  UNIQUE NOT NULL,
    email         VARCHAR(255),
    password_hash VARCHAR(255) NOT NULL,
    role          VARCHAR(20)  NOT NULL DEFAULT 'viewer',
                  -- Valid roles: full_admin (app owner, exactly 1),
                  --              admin (own-resource CRUD),
                  --              viewer (read-only, sees all)
    is_active     BOOLEAN DEFAULT true,
    created_at    TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at    TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_login    TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    token VARCHAR(255) UNIQUE NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    ip_address VARCHAR(45),
    user_agent TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    username VARCHAR(50),
    action VARCHAR(100) NOT NULL,
    resource_type VARCHAR(50),
    resource_id VARCHAR(255),
    details JSONB,
    ip_address VARCHAR(45),
    user_agent TEXT,
    success BOOLEAN DEFAULT true,
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_username      ON users(username);
CREATE INDEX IF NOT EXISTS idx_users_email         ON users(email);
CREATE INDEX IF NOT EXISTS idx_sessions_token      ON sessions(token);
CREATE INDEX IF NOT EXISTS idx_sessions_user_id    ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_audit_log_user_id   ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_log_action    ON audit_log(action);

CREATE TABLE IF NOT EXISTS settings (
    key        VARCHAR(100) PRIMARY KEY,
    value      TEXT NOT NULL DEFAULT '',
    description TEXT,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS patch_schedules (
    id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                      VARCHAR(100) NOT NULL,
    enabled                   BOOLEAN DEFAULT TRUE,
    day_of_week               TEXT NOT NULL DEFAULT 'sunday',
    start_time                TIME NOT NULL DEFAULT '02:00',
    end_time                  TIME NOT NULL DEFAULT '04:00',
    auto_reboot               BOOLEAN DEFAULT FALSE,
    become_password_encrypted TEXT,
    retry_host_ids            UUID[],
    created_at                TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at                TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_run                  TIMESTAMP WITH TIME ZONE,
    last_status               TEXT,
    created_by                UUID REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS patch_schedule_hosts (
    schedule_id UUID REFERENCES patch_schedules(id) ON DELETE CASCADE,
    host_id     UUID REFERENCES hosts(id) ON DELETE CASCADE,
    PRIMARY KEY (schedule_id, host_id)
);

CREATE TABLE IF NOT EXISTS saved_ssh_keys (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(100) NOT NULL,
    private_key TEXT NOT NULL,
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    created_by  UUID REFERENCES users(id) ON DELETE SET NULL
);
