-- PatchPilot Settings Interface - Database Migration
-- Version: 2.0.0
-- Date: 2026-02-06

-- Add new columns to hosts table for SSH management
ALTER TABLE hosts ADD COLUMN IF NOT EXISTS ssh_key_type VARCHAR(20) DEFAULT 'default';
ALTER TABLE hosts ADD COLUMN IF NOT EXISTS ssh_private_key_encrypted BYTEA;
ALTER TABLE hosts ADD COLUMN IF NOT EXISTS ssh_password_encrypted BYTEA;
ALTER TABLE hosts ADD COLUMN IF NOT EXISTS ssh_user VARCHAR(100) DEFAULT 'root';
ALTER TABLE hosts ADD COLUMN IF NOT EXISTS ssh_port INTEGER DEFAULT 22;
ALTER TABLE hosts ADD COLUMN IF NOT EXISTS notes TEXT;
ALTER TABLE hosts ADD COLUMN IF NOT EXISTS tags VARCHAR(255);
ALTER TABLE hosts ADD COLUMN IF NOT EXISTS is_control_node BOOLEAN DEFAULT FALSE;
ALTER TABLE hosts ADD COLUMN IF NOT EXISTS allow_auto_reboot BOOLEAN DEFAULT TRUE;
ALTER TABLE hosts ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE hosts ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;

-- Create settings table for application-wide settings
CREATE TABLE IF NOT EXISTS settings (
    key VARCHAR(100) PRIMARY KEY,
    value TEXT,
    description TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Insert default settings
INSERT INTO settings (key, value, description) VALUES 
    ('auto_refresh_interval', '300', 'Auto-refresh interval in seconds (default: 5 minutes)')
ON CONFLICT (key) DO NOTHING;

INSERT INTO settings (key, value, description) VALUES 
    ('default_ssh_user', 'root', 'Default SSH user for new hosts')
ON CONFLICT (key) DO NOTHING;

INSERT INTO settings (key, value, description) VALUES 
    ('default_ssh_port', '22', 'Default SSH port for new hosts')
ON CONFLICT (key) DO NOTHING;

INSERT INTO settings (key, value, description) VALUES 
    ('encryption_enabled', 'true', 'Enable encryption for stored credentials')
ON CONFLICT (key) DO NOTHING;

-- Create audit log table for tracking changes
CREATE TABLE IF NOT EXISTS audit_log (
    id SERIAL PRIMARY KEY,
    action VARCHAR(50) NOT NULL,
    resource_type VARCHAR(50) NOT NULL,
    resource_id VARCHAR(100),
    user_info TEXT,
    details JSONB,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Create index for faster lookups
CREATE INDEX IF NOT EXISTS idx_hosts_hostname ON hosts(hostname);
CREATE INDEX IF NOT EXISTS idx_hosts_status ON hosts(status);
CREATE INDEX IF NOT EXISTS idx_hosts_control_node ON hosts(is_control_node);
CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_log_resource ON audit_log(resource_type, resource_id);

-- Add trigger to update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ language 'plpgsql';

DROP TRIGGER IF EXISTS update_hosts_updated_at ON hosts;
CREATE TRIGGER update_hosts_updated_at
    BEFORE UPDATE ON hosts
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_settings_updated_at ON settings;
CREATE TRIGGER update_settings_updated_at
    BEFORE UPDATE ON settings
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- Comments for documentation
COMMENT ON COLUMN hosts.ssh_key_type IS 'Type of SSH authentication: default, file, pasted, password';
COMMENT ON COLUMN hosts.ssh_private_key_encrypted IS 'Encrypted SSH private key (Fernet encrypted)';
COMMENT ON COLUMN hosts.ssh_password_encrypted IS 'Encrypted SSH password (Fernet encrypted, not recommended)';
COMMENT ON COLUMN hosts.is_control_node IS 'TRUE if this host runs PatchPilot Docker containers';
COMMENT ON TABLE settings IS 'Application-wide configuration settings';
COMMENT ON TABLE audit_log IS 'Audit trail for all settings and host management actions';

-- Migration complete
SELECT 'Settings interface database migration completed successfully' AS status;
