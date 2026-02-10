-- Hosts table
CREATE TABLE IF NOT EXISTS hosts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    hostname VARCHAR(255) UNIQUE NOT NULL,
    ip_address VARCHAR(45),
    os_type VARCHAR(50),
    os_family VARCHAR(50),
    last_checked TIMESTAMP WITH TIME ZONE,
    status VARCHAR(50),
    total_updates INTEGER DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Packages table
CREATE TABLE IF NOT EXISTS packages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    host_id UUID REFERENCES hosts(id) ON DELETE CASCADE,
    package_name VARCHAR(255) NOT NULL,
    current_version VARCHAR(100),
    available_version VARCHAR(100),
    update_type VARCHAR(50),
    detected_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(host_id, package_name)
);

-- Patch history table
CREATE TABLE IF NOT EXISTS patch_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    host_id UUID REFERENCES hosts(id) ON DELETE CASCADE,
    execution_time TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    packages_updated TEXT[],
    success BOOLEAN,
    error_message TEXT,
    duration_seconds INTEGER
);

-- Create indexes for better performance
CREATE INDEX IF NOT EXISTS idx_hosts_hostname ON hosts(hostname);
CREATE INDEX IF NOT EXISTS idx_hosts_status ON hosts(status);
CREATE INDEX IF NOT EXISTS idx_packages_host_id ON packages(host_id);
CREATE INDEX IF NOT EXISTS idx_patch_history_host_id ON patch_history(host_id);
CREATE INDEX IF NOT EXISTS idx_patch_history_execution_time ON patch_history(execution_time);

-- Enable Row Level Security (optional but recommended)
ALTER TABLE hosts ENABLE ROW LEVEL SECURITY;
ALTER TABLE packages ENABLE ROW LEVEL SECURITY;
ALTER TABLE patch_history ENABLE ROW LEVEL SECURITY;

-- Create policies (adjust based on your auth setup)
-- For now, allow all operations (you can restrict this later)
CREATE POLICY "Allow all operations on hosts" ON hosts FOR ALL USING (true);
CREATE POLICY "Allow all operations on packages" ON packages FOR ALL USING (true);
CREATE POLICY "Allow all operations on patch_history" ON patch_history FOR ALL USING (true);
