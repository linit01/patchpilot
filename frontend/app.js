// API Configuration
const API_BASE_URL = '/api';  // Use relative path in production

// WebSocket Configuration
const WS_BASE_URL = `ws${window.location.protocol === 'https:' ? 's' : ''}://${window.location.host}`;

// Auth State
let currentUser = null;
let isAuthenticated = false;

// ── Global 401 interceptor ──────────────────────────────────────────────────
// When the session cookie expires mid-use, API calls start returning 401
// but the dashboard keeps showing stale data. This intercepts 401 responses
// and redirects to the login page instead of silently failing.
//
// Track consecutive 401s — a single 401 on a role-gated endpoint (like
// /auth/users for non-admins) is normal. Multiple 401s in a row means
// the session has actually expired.
let _consecutive401s = 0;
const _AUTH_REDIRECT_THRESHOLD = 3;

const _originalFetch = window.fetch;
window.fetch = async function(...args) {
    const response = await _originalFetch.apply(this, args);
    if (response.status === 401 && isAuthenticated) {
        const url = typeof args[0] === 'string' ? args[0] : args[0]?.url || '';
        // Don't count auth-check endpoints or user-management endpoints
        // which legitimately return 401 for non-admin roles
        if (!url.includes('/auth/me') && !url.includes('/auth/users')) {
            _consecutive401s++;
            if (_consecutive401s >= _AUTH_REDIRECT_THRESHOLD) {
                console.warn('Session expired — redirecting to login');
                isAuthenticated = false;
                currentUser = null;
                window.location.href = 'login.html';
            }
        }
    } else if (response.ok) {
        _consecutive401s = 0;
    }
    return response;
};

// RBAC: owner filter for full_admin user dropdown
let _ownerFilter = '';  // empty = all users; UUID = specific user

function _ownerParam(sep) {
    // Returns '?owner=UUID' or '&owner=UUID' or '' depending on filter state
    if (!_ownerFilter) return '';
    return `${sep}owner=${_ownerFilter}`;
}

// WebSocket for real-time patch progress
let patchProgressWS = null;

// Auto-check countdown (reads from settings, default 2 minutes)
let AUTO_CHECK_INTERVAL = 120;
let countdownSeconds = AUTO_CHECK_INTERVAL;
let countdownInterval = null;

function connectPatchProgressWebSocket() {
    // Close stale connections
    if (patchProgressWS) {
        if (patchProgressWS.readyState === WebSocket.OPEN) {
            return; // Already connected and open
        }
        // CONNECTING, CLOSING, or CLOSED — tear down and reconnect
        try { patchProgressWS.close(); } catch(e) {}
        patchProgressWS = null;
    }
    
    try {
        patchProgressWS = new WebSocket(`${WS_BASE_URL}/ws/patch-progress`);
        
        patchProgressWS.onopen = () => {
            console.log('Patch progress WebSocket connected');
        };
        
        patchProgressWS.onmessage = (event) => {
            const data = JSON.parse(event.data);
            handlePatchProgress(data);
        };
        
        patchProgressWS.onerror = (error) => {
            console.error('Patch progress WebSocket error:', error);
        };
        
        patchProgressWS.onclose = () => {
            console.log('Patch progress WebSocket closed');
            patchProgressWS = null;
            // If the patch progress modal is still open, the WS dropped mid-patch.
            // Start polling /api/patch/status so we can close the modal when done.
            const modal = document.getElementById('patch-progress-modal');
            if (modal && modal.style.display === 'flex') {
                _startPatchStatusPolling();
            }
        };
    } catch (e) {
        console.error('Failed to create WebSocket:', e);
        patchProgressWS = null;
    }
}

// Polling fallback — used when WebSocket drops during a long patch (e.g. large
// App Store downloads). Polls /api/patch/status every 10 s and closes the modal
// once the backend reports the patch is no longer running.
let _patchStatusPollTimer = null;
function _startPatchStatusPolling() {
    if (_patchStatusPollTimer) return; // already polling
    console.log('WS dropped mid-patch — starting poll fallback');
    const progressDiv = document.getElementById('patch-progress-messages');
    if (progressDiv) {
        const el = document.createElement('div');
        el.className = 'progress-message msg-warning';
        el.textContent = `[${new Date().toLocaleTimeString()}] ⚠️ Connection interrupted — monitoring via polling...`;
        progressDiv.appendChild(el);
        progressDiv.scrollTop = progressDiv.scrollHeight;
    }
    _patchStatusPollTimer = setInterval(async () => {
        try {
            const res = await fetch('/api/patch/status', { credentials: 'include' });
            if (!res.ok) return;
            const data = await res.json();
            if (!data.running) {
                // Patch + post-check are both done
                clearInterval(_patchStatusPollTimer);
                _patchStatusPollTimer = null;
                handlePatchProgress({
                    type: 'complete',
                    message: 'All operations complete! (recovered via polling)'
                });
            } else if (data.patch_running === false && data.check_running === true) {
                // Patch done, post-check running — update UI
                const progressDiv = document.getElementById('patch-progress-messages');
                if (progressDiv && !progressDiv.querySelector('._poll-check-msg')) {
                    const el = document.createElement('div');
                    el.className = 'progress-message msg-info _poll-check-msg';
                    el.textContent = `[${new Date().toLocaleTimeString()}] 🔍 Patch complete — running post-patch status check...`;
                    progressDiv.appendChild(el);
                    progressDiv.scrollTop = progressDiv.scrollHeight;
                }
            }
        } catch (e) {
            console.warn('Patch status poll failed:', e);
        }
    }, 10000); // poll every 10 s
}

function handlePatchProgress(data) {
    const progressDiv = document.getElementById('patch-progress-messages');
    const hostsDiv = document.getElementById('patch-progress-hosts');
    if (!progressDiv) return;
    
    const timestamp = new Date().toLocaleTimeString();
    let message = '';
    let msgClass = 'progress-message';
    
    switch(data.type) {
        case 'start':
            message = `[${timestamp}] ${data.message}`;
            msgClass += ' msg-start';
            // Build per-host status badges only if not already shown
            if (data.hosts && hostsDiv && hostsDiv.children.length === 0) {
                hostsDiv.innerHTML = data.hosts.map(h => 
                    `<span id="patch-host-${h.replace(/[^a-zA-Z0-9]/g, '_')}" 
                           style="display:inline-flex;align-items:center;gap:6px;padding:6px 12px;border-radius:6px;font-size:13px;font-weight:600;background:var(--bg-dark);border:1px solid var(--border);color:var(--text-muted);">
                        <span class="host-status-icon">⏳</span> ${h}
                    </span>`
                ).join('');
            }
            // Show modal only if not already visible
            if (document.getElementById('patch-progress-modal').style.display !== 'flex') {
                showPatchProgressModal();
            }
            break;
        case 'progress':
            message = `[${timestamp}] ${data.message}`;
            // Update per-host badge when we detect host-specific activity
            if (data.hostname && hostsDiv) {
                const safeId = data.hostname.replace(/[^a-zA-Z0-9]/g, '_');
                const badge = document.getElementById(`patch-host-${safeId}`);
                if (badge) {
                    badge.style.borderColor = 'var(--blue)';
                    badge.style.color = 'var(--blue)';
                    badge.querySelector('.host-status-icon').textContent = '🔧';
                }
            }
            // Color-code based on content
            if (data.message.startsWith('TASK [')) msgClass += ' msg-task';
            else if (data.message.startsWith('📦')) msgClass += ' msg-package';
            else if (data.message.startsWith('✅')) msgClass += ' msg-success';
            else if (data.message.startsWith('📡') || data.message.startsWith('📥')) msgClass += ' msg-download';
            else if (data.message.startsWith('📋') || data.message.startsWith('🔍')) msgClass += ' msg-info';
            else if (data.message.startsWith('🔧') || data.message.startsWith('⚙️') || data.message.startsWith('🔄')) msgClass += ' msg-config';
            else if (data.message.startsWith('🐧') || data.message.startsWith('🐳')) msgClass += ' msg-info';
            else if (data.message.startsWith('⏱️')) msgClass += ' msg-timing';
            else if (data.message.startsWith('⚠️')) msgClass += ' msg-warning';
            else if (data.message.startsWith('❌')) msgClass += ' msg-error';
            else if (data.message.includes('skipping:')) msgClass += ' msg-skip';
            else if (data.message.includes('PLAY RECAP')) msgClass += ' msg-task';
            break;
        case 'success':
            message = `[${timestamp}] ✅ ${data.message}`;
            msgClass += ' msg-success';
            // Mark all host badges as success
            if (hostsDiv) {
                hostsDiv.querySelectorAll('[id^="patch-host-"]').forEach(badge => {
                    badge.style.borderColor = 'var(--green-bright)';
                    badge.style.color = 'var(--green-bright)';
                    badge.querySelector('.host-status-icon').textContent = '✅';
                });
            }
            break;
        case 'complete':
            message = `[${timestamp}] 🎉 ${data.message}`;
            msgClass += ' msg-complete';
            setTimeout(() => {
                closePatchProgressModal();
                loadHosts(); // Refresh
            }, 3000);
            break;
        case 'error':
            message = `[${timestamp}] ❌ ${data.message}`;
            msgClass += ' msg-error';
            // Mark host badges as error if we know which host
            if (data.hostname && hostsDiv) {
                const safeId = data.hostname.replace(/[^a-zA-Z0-9]/g, '_');
                const badge = document.getElementById(`patch-host-${safeId}`);
                if (badge) {
                    badge.style.borderColor = 'var(--red)';
                    badge.style.color = 'var(--red)';
                    badge.querySelector('.host-status-icon').textContent = '❌';
                }
            }
            break;
    }
    
    if (message) {
        const msgEl = document.createElement('div');
        msgEl.className = msgClass;
        msgEl.textContent = message;
        progressDiv.appendChild(msgEl);
        progressDiv.scrollTop = progressDiv.scrollHeight;
    }
}

// State
let hostsData = [];
let selectedHosts = new Set();

// Countdown Timer Functions
function startCountdown() {
    if (countdownInterval) {
        clearInterval(countdownInterval);
        countdownInterval = null;
    }

    // If auto-check is disabled, hide the timer and don't start the loop
    if (AUTO_CHECK_INTERVAL === 0) {
        const element = document.getElementById('countdown-timer');
        if (element) element.textContent = 'Disabled';
        sessionStorage.removeItem('patchpilot-countdown');
        sessionStorage.removeItem('patchpilot-countdown-time');
        return;
    }

    // Restore countdown from sessionStorage if navigating back
    const saved = sessionStorage.getItem('patchpilot-countdown');
    const savedTime = sessionStorage.getItem('patchpilot-countdown-time');
    if (saved && savedTime) {
        const elapsed = Math.floor((Date.now() - parseInt(savedTime)) / 1000);
        const remaining = parseInt(saved) - elapsed;
        countdownSeconds = remaining > 0 ? remaining : AUTO_CHECK_INTERVAL;
    } else {
        countdownSeconds = AUTO_CHECK_INTERVAL;
    }
    updateCountdownDisplay();
    
    countdownInterval = setInterval(() => {
        countdownSeconds--;
        updateCountdownDisplay();
        // Persist to sessionStorage for page navigation
        sessionStorage.setItem('patchpilot-countdown', countdownSeconds);
        sessionStorage.setItem('patchpilot-countdown-time', Date.now());
        
        if (countdownSeconds <= 0) {
            countdownSeconds = AUTO_CHECK_INTERVAL;
            // Trigger a real Ansible scan (same as REFRESH button) — not just a DB poll.
            // The user's "Next check" countdown should mean an actual update scan.
            triggerCheckAndPoll();
            // Also re-check for app updates so the sidebar badge appears
            // without requiring a page refresh
            checkForUpdateBadge();
        }
    }, 1000);
}

function updateCountdownDisplay() {
    const minutes = Math.floor(countdownSeconds / 60);
    const seconds = countdownSeconds % 60;
    const display = `${minutes}:${seconds.toString().padStart(2, '0')}`;
    const element = document.getElementById('countdown-timer');
    if (element) {
        element.textContent = display;
    }
}

function resetCountdown() {
    countdownSeconds = AUTO_CHECK_INTERVAL;
    sessionStorage.setItem('patchpilot-countdown', countdownSeconds);
    sessionStorage.setItem('patchpilot-countdown-time', Date.now());
    updateCountdownDisplay();
}
// Initialize dashboard on load
document.addEventListener('DOMContentLoaded', () => {
    checkAuthAndInit();
});

// Check authentication and initialize
async function checkAuthAndInit() {
    // ── Setup guard: redirect to wizard if no users exist ──────────────────
    try {
        const setupRes = await fetch(`${API_BASE_URL}/auth/check-setup`);
        const setupData = await setupRes.json();
        if (setupData.setup_required || !setupData.has_users) {
            window.location.replace('setup.html');
            return;
        }
    } catch (e) {
        // backend not ready yet — continue and let auth check handle it
    }

    try {
        const res = await fetch(`${API_BASE_URL}/auth/me`, { credentials: 'include' });
        const data = await res.json();
        
        if (data.authenticated) {
            isAuthenticated = true;
            currentUser = data.user;
            showAuthenticatedUI();
            populateOwnerFilter();
        } else {
            isAuthenticated = false;
            currentUser = null;
            showUnauthenticatedUI();
        }
    } catch (e) {
        console.error('Auth check failed:', e);
        isAuthenticated = false;
        showUnauthenticatedUI();
    }
    
    // Always load dashboard (public read-only data)
    initializeEventListeners();
    loadDashboard();
    await fetchRefreshInterval();
    startCountdown();
    // Fetch real version from API and update the sidebar version tag
    fetch(`${API_BASE_URL}/`)
        .then(r => r.json())
        .then(d => {
            if (d.version) {
                const el = document.getElementById('sidebar-app-version');
                // Strip "-alpha"/"-beta" suffix — the HTML has a separate span for that
                const ver = d.version.replace(/-(alpha|beta).*$/i, '');
                if (el) el.textContent = 'v' + ver;
            }
        })
        .catch(() => {}); // silently keep the hardcoded fallback
    // Check for available updates and show sidebar badge
    checkForUpdateBadge();
    // Note: startCountdown already handles periodic loadDashboard at countdown=0
    // No duplicate setInterval needed
}

// Fetch refresh interval from settings
async function fetchRefreshInterval() {
    try {
        const response = await fetch(`${API_BASE_URL}/settings/app`, { credentials: 'include' });
        if (response.ok) {
            const settings = await response.json();
            if (settings.refresh_interval && settings.refresh_interval.value !== undefined) {
                const val = parseInt(settings.refresh_interval.value);
                if (val === 0) {
                    AUTO_CHECK_INTERVAL = 0;  // Disabled
                    console.log('Refresh interval: disabled');
                } else {
                    AUTO_CHECK_INTERVAL = Math.max(val, 30);  // Floor at 30s
                    console.log(`Refresh interval set to ${AUTO_CHECK_INTERVAL}s`);
                }
            }
        }
    } catch (e) {
        console.log('Could not fetch refresh interval, using default');
    }
}

// Show UI for authenticated users
function showAuthenticatedUI() {
    const sidebarUser = document.getElementById('sidebar-user');
    const sidebarLogin = document.getElementById('sidebar-login');
    const mgmtLabel = document.getElementById('mgmt-section-label');
    const refreshBtn = document.getElementById('refresh-btn');
    const patchBtn = document.getElementById('patch-selected-btn');

    // ── RBAC: determine which nav items to show based on role ──────────
    const role = currentUser.role;

    // Nav items every authenticated user sees
    const alwaysShow = [];

    // Nav items for write-capable users (full_admin + admin)
    const writeNav = ['nav-hosts-mgmt', 'nav-ssh-keys', 'nav-schedules'];

    // Nav items only full_admin sees
    const fullAdminNav = ['nav-general', 'nav-users', 'nav-advanced'];

    // All management nav IDs (for hiding)
    const allMgmtNavIds = [...writeNav, ...fullAdminNav];

    if (sidebarUser) {
        sidebarUser.style.display = 'flex';
        document.getElementById('sidebar-username').textContent = currentUser.username;
        const roleLabels = { full_admin: 'Full Admin', admin: 'Admin', viewer: 'Viewer' };
        document.getElementById('sidebar-role').textContent = roleLabels[role] || role;
        const initials = currentUser.username.substring(0, 2).toUpperCase();
        document.getElementById('user-avatar-initials').textContent = initials;
    }
    if (sidebarLogin) sidebarLogin.style.display = 'none';

    // Start with everything hidden
    if (mgmtLabel) mgmtLabel.style.display = 'none';
    allMgmtNavIds.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.style.display = 'none';
    });

    if (role === 'viewer') {
        // Viewer: no management section, no action buttons
        if (refreshBtn) refreshBtn.style.display = 'none';
        if (patchBtn) patchBtn.style.display = 'none';
    } else if (role === 'admin') {
        // Admin: show write nav items, hide full_admin items
        if (mgmtLabel) mgmtLabel.style.display = '';
        writeNav.forEach(id => {
            const el = document.getElementById(id);
            if (el) el.style.display = '';
        });
        if (refreshBtn) refreshBtn.style.display = '';
        if (patchBtn) patchBtn.style.display = '';
    } else {
        // full_admin: show everything
        if (mgmtLabel) mgmtLabel.style.display = '';
        allMgmtNavIds.forEach(id => {
            const el = document.getElementById(id);
            if (el) el.style.display = '';
        });
        if (refreshBtn) refreshBtn.style.display = '';
        if (patchBtn) patchBtn.style.display = '';
    }

    // Enable checkboxes only for write-capable roles
    if (role !== 'viewer') {
        document.querySelectorAll('.host-checkbox, #select-all').forEach(cb => {
            cb.disabled = false;
        });
    }
}

// Show UI for unauthenticated users (read-only)
function showUnauthenticatedUI() {
    const sidebarUser = document.getElementById('sidebar-user');
    const sidebarLogin = document.getElementById('sidebar-login');
    const mgmtLabel = document.getElementById('mgmt-section-label');
    const refreshBtn = document.getElementById('refresh-btn');
    const patchBtn = document.getElementById('patch-selected-btn');
    const mgmtNavIds = ['nav-hosts-mgmt','nav-general','nav-ssh-keys','nav-users','nav-schedules','nav-advanced'];

    if (sidebarUser) sidebarUser.style.display = 'none';
    if (sidebarLogin) sidebarLogin.style.display = 'flex';
    if (mgmtLabel) mgmtLabel.style.display = 'none';
    mgmtNavIds.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.style.display = 'none';
    });
    if (refreshBtn) refreshBtn.style.display = 'none';
    if (patchBtn) patchBtn.style.display = 'none';
}

// ── RBAC: Owner filter dropdown for full_admin ─────────────────────────
async function populateOwnerFilter() {
    const select = document.getElementById('owner-filter');
    if (!select || !currentUser || currentUser.role !== 'full_admin') return;
    try {
        const res = await fetch(`${API_BASE_URL}/auth/users`, { credentials: 'include' });
        if (!res.ok) return;
        const users = await res.json();
        // Only show if there's more than one user
        if (users.length <= 1) return;
        select.innerHTML = '<option value="">All Users</option>';
        const roleLabels = { full_admin: 'Full Admin', admin: 'Admin', viewer: 'Viewer' };
        users.forEach(u => {
            if (u.role === 'viewer') return;  // viewers don't own resources
            const label = `${u.username} (${roleLabels[u.role] || u.role})`;
            select.innerHTML += `<option value="${u.id}">${label}</option>`;
        });
        select.style.display = '';
    } catch (e) {
        console.log('Could not populate owner filter:', e);
    }
}

function applyOwnerFilter(value) {
    _ownerFilter = value;
    // Re-fetch all dashboard data with new filter
    loadStats();
    loadHosts();
    loadChartData();
    loadSidebarStats();
}

// Handle logout
async function handleLogout() {
    try {
        await fetch(`${API_BASE_URL}/auth/logout`, {
            method: 'POST',
            credentials: 'include'
        });
    } catch (e) {
        console.error('Logout error:', e);
    }
    window.location.href = 'login.html';
}

// Event Listeners
function initializeEventListeners() {
    document.getElementById('refresh-btn').addEventListener('click', handleRefresh);
    document.getElementById('patch-selected-btn').addEventListener('click', handlePatchSelected);
    document.getElementById('select-all').addEventListener('click', handleSelectAll);
}

// Load Dashboard Data
async function loadDashboard() {
    try {
        await Promise.all([
            loadStats(),
            loadHosts(),
            loadChartData(),
            loadSidebarStats()
        ]);
    } catch (error) {
        showStatus('Error loading dashboard: ' + error.message, 'error');
    }
}

// Load Sidebar Stats (load avg, uptime, badges)
async function loadSidebarStats() {
    try {
        const response = await fetch(`${API_BASE_URL}/stats/sidebar?t=${Date.now()}${_ownerParam('&')}`, {
            cache: 'no-store'
        });
        if (!response.ok) return;
        const data = await response.json();
        
        // Load average
        const loadEl = document.getElementById('sidebar-load');
        if (loadEl) {
            loadEl.textContent = `Load: ${data.load_1} / ${data.load_5} / ${data.load_15}`;
        }
        // Color the load dot based on load average
        const loadDot = document.getElementById('sidebar-load-dot');
        if (loadDot) {
            if (data.load_1 > 4) loadDot.className = 'status-dot red';
            else if (data.load_1 > 1.5) loadDot.className = 'status-dot amber';
            else loadDot.className = 'status-dot green';
        }
        
        // Uptime
        const uptimeEl = document.getElementById('sidebar-uptime');
        if (uptimeEl) {
            uptimeEl.textContent = `Uptime: ${data.uptime}`;
        }
        
        // Badge: Hosts
        updateBadge('sidebar-hosts-badge', data.host_count);
        // Badge: Packages
        updateBadge('sidebar-packages-badge', data.package_count);
        // Badge: Patch History
        updateBadge('sidebar-history-badge', data.history_count);
        // Badge: Alerts
        updateBadge('sidebar-alerts-badge', data.alert_count);

        // Update available badge — piggyback on sidebar refresh so it stays current
        checkForUpdateBadge();
        
    } catch (error) {
        console.error('Error loading sidebar stats:', error);
    }
}

function updateBadge(elementId, count) {
    const el = document.getElementById(elementId);
    if (!el) return;
    if (count > 0) {
        el.textContent = count;
        el.style.display = '';
    } else {
        el.style.display = 'none';
    }
}

// Scroll to section when clicking sidebar nav
function scrollToSection(sectionId) {
    const el = document.getElementById(sectionId);
    if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
}

// Load Statistics
async function loadStats() {
    try {
        const response = await fetch(`${API_BASE_URL}/stats?t=${Date.now()}${_ownerParam('&')}`, {
            cache: 'no-store',
            headers: {
                'Cache-Control': 'no-cache, no-store, must-revalidate',
                'Pragma': 'no-cache'
            }
        });
        const stats = await response.json();
        
        document.getElementById('total-hosts').textContent = stats.total_hosts;
        document.getElementById('up-to-date').textContent = stats.up_to_date;
        document.getElementById('need-updates').textContent = stats.need_updates;
        document.getElementById('unreachable').textContent = stats.unreachable;
        document.getElementById('total-updates').textContent = stats.total_pending_updates;
        
        // Update sidebar
        const sidebarCount = document.getElementById('sidebar-host-count');
        if (sidebarCount) sidebarCount.textContent = `${stats.total_hosts} hosts monitored`;
        
        // Update table subtitle
        const subtitle = document.getElementById('table-subtitle');
        if (subtitle) subtitle.textContent = `${stats.total_hosts} systems · ${stats.total_pending_updates} pending updates`;
        
        // Update sidebar status dot based on unreachable count
        const sidebarHostDot = document.getElementById('sidebar-host-dot');
        if (sidebarHostDot) {
            sidebarHostDot.className = 'status-dot ' + (stats.unreachable > 0 ? (stats.unreachable === stats.total_hosts ? 'red' : 'amber') : 'green');
        }
    } catch (error) {
        console.error('Error loading stats:', error);
    }
}

// Load Hosts
async function loadHosts() {
    try {
        const response = await fetch(`${API_BASE_URL}/hosts?t=${Date.now()}${_ownerParam('&')}`, {
            cache: 'no-store',
            headers: {
                'Cache-Control': 'no-cache, no-store, must-revalidate',
                'Pragma': 'no-cache'
            }
        });
        const parsed = await response.json();
        hostsData = Array.isArray(parsed) ? parsed : [];

        // Track newest last_checked across all hosts so triggerCheckAndPoll
        // knows when fresh Ansible results have landed in the DB.
        const newestCheck = hostsData.reduce((max, h) => {
            const t = h.last_checked ? new Date(h.last_checked).getTime() : 0;
            return t > max ? t : max;
        }, 0);
        if (newestCheck > _lastCheckTimestamp) _lastCheckTimestamp = newestCheck;

        renderHostsTable();
    } catch (error) {
        console.error('Error loading hosts:', error);
        showStatus('Error loading hosts: ' + error.message, 'error');
    }
}

// Render Hosts Table
function renderHostsTable() {
    const tbody = document.getElementById('hosts-table-body');
    const isFullAdmin = isAuthenticated && currentUser && currentUser.role === 'full_admin';
    const colSpan = isFullAdmin ? 9 : 8;
    
    // Dynamically add/remove Owner header column
    const thead = tbody.closest('table').querySelector('thead tr');
    const existingOwnerTh = thead.querySelector('.owner-col');
    if (isFullAdmin && !existingOwnerTh) {
        const th = document.createElement('th');
        th.className = 'owner-col';
        th.textContent = 'Owner';
        thead.insertBefore(th, thead.children[thead.children.length - 1]); // before Actions
    } else if (!isFullAdmin && existingOwnerTh) {
        existingOwnerTh.remove();
    }
    
    if (hostsData.length === 0) {
        tbody.innerHTML = `<tr><td colspan="${colSpan}" class="loading">No hosts found</td></tr>`;
        return;
    }
    
    const canWrite = isAuthenticated && currentUser && currentUser.role !== 'viewer';
    
    tbody.innerHTML = hostsData.map(host => {
        const isSelected = selectedHosts.has(host.hostname);
        const statusClass = getStatusClass(host.status);
        const lastChecked = host.last_checked 
            ? new Date(host.last_checked).toLocaleTimeString()
            : 'Never';
        const ownerCol = isFullAdmin
            ? `<td><span style="font-size:12px;color:var(--text-secondary)">${host.owner_username || '—'}</span></td>`
            : '';
        
        return `
            <tr class="${isSelected ? 'selected' : ''}" data-hostname="${host.hostname}">
                <td>
                    <input 
                        type="checkbox" 
                        class="host-checkbox" 
                        data-hostname="${host.hostname}"
                        ${isSelected ? 'checked' : ''}
                        ${!canWrite ? 'disabled' : ''}
                        onchange="handleHostCheckbox('${host.hostname}')"
                    />
                </td>
                <td>
                    <strong style="color:var(--text-primary)">${host.hostname}</strong>
                    ${host.is_control_node ? '<span class="control-node-badge">CONTROL</span>' : ''}
                </td>
                <td><span style="font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--text-muted)">${host.ip_address || 'N/A'}</span></td>
                <td>${host.os_family || 'Unknown'}</td>
                <td>
                    <span class="status-badge status-${statusClass}">
                        ${host.status === 'up-to-date' ? '✓' : host.status === 'updates-available' ? '⚠' : '✕'} ${host.status}
                    </span>
                </td>
                <td>
                    ${host.total_updates > 0 
                        ? `<span style="font-family:'JetBrains Mono',monospace;font-weight:600;color:var(--amber)">${host.total_updates} pkg${host.total_updates > 1 ? 's' : ''}</span>`
                        : '<span style="color:var(--text-muted)">—</span>'
                    }
                </td>
                <td><span style="font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--text-muted)">${lastChecked}</span></td>
                ${ownerCol}
                <td>
                    <button class="action-btn" onclick="showHostDetails('${host.hostname}')">
                        Details
                    </button>
                </td>
            </tr>
        `;
    }).join('');
    
    updateSelectedCount();
}

// Get Status CSS Class
function getStatusClass(status) {
    const statusMap = {
        'up-to-date': 'up-to-date',
        'updates-available': 'updates-available',
        'unreachable': 'unreachable',
        'unknown': 'unknown'
    };
    return statusMap[status] || 'unknown';
}

// Handle Refresh
// Shared function used by both the REFRESH button and the countdown timer.
// Triggers a real Ansible scan via POST /api/check, then polls until the
// lock clears and updates the dashboard. Prevents the two entry-points from
// having divergent behaviour.
async function triggerCheckAndPoll() {
    if (!isAuthenticated) return;

    const btn = document.getElementById('refresh-btn');
    const timerEl = document.getElementById('countdown-timer');

    // Disable REFRESH button and freeze countdown display while scanning
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="btn-icon">⏳</span> Scanning...'; }
    if (timerEl) timerEl.textContent = '⏳ scanning';

    try {
        const response = await fetch(`${API_BASE_URL}/check`, {
            method: 'POST',
            credentials: 'include'
        });

        if (!response.ok) throw new Error('Failed to trigger check');

        const data = await response.json();
        const queued = data.status === 'queued';

        document.dispatchEvent(new CustomEvent('patchpilot:refresh-start'));

        // Poll the backend every 5 s. Stop when:
        //  - a host's last_checked timestamp is newer than when we started, OR
        //  - we've waited up to 5 minutes (fallback)
        const startedAt = Date.now();
        const MAX_WAIT_MS = 300000; // 5 min
        const pollInterval = setInterval(async () => {
            await loadDashboard();
            const elapsed = Date.now() - startedAt;
            const anyUpdated = _lastCheckTimestamp && _lastCheckTimestamp > startedAt;
            if (anyUpdated || elapsed >= MAX_WAIT_MS) {
                clearInterval(pollInterval);
                if (btn) { btn.disabled = false; btn.innerHTML = '<span class="btn-icon">🔄</span> Refresh Status'; }
                resetCountdown();
                document.dispatchEvent(new CustomEvent('patchpilot:refresh-done'));
            }
        }, 5000);
    } catch (error) {
        showStatus('Error triggering check: ' + error.message, 'error');
        if (btn) { btn.disabled = false; btn.innerHTML = '<span class="btn-icon">🔄</span> Refresh Status'; }
        resetCountdown();
    }
}

// Track the most recent last_checked timestamp seen across all hosts so
// triggerCheckAndPoll knows when new results have arrived from Ansible.
let _lastCheckTimestamp = 0;

async function handleRefresh() {
    if (!isAuthenticated) {
        showStatus('Please sign in to trigger a refresh', 'error');
        return;
    }
    resetCountdown();
    triggerCheckAndPoll();
}

async function _handleRefresh_UNUSED() {
    // kept for reference — replaced by triggerCheckAndPoll above
    if (!isAuthenticated) {
        showStatus('Please sign in to trigger a refresh', 'error');
        return;
    }
    
    const btn = document.getElementById('refresh-btn');
    btn.disabled = true;
    resetCountdown();
    btn.innerHTML = '<span class="btn-icon">⏳</span> Checking...';
    
    try {
        const response = await fetch(`${API_BASE_URL}/check`, {
            method: 'POST',
            credentials: 'include'
        });
        
        if (response.ok) {
            showStatus('Update check initiated. Waiting for completion...', 'success');
            document.dispatchEvent(new CustomEvent('patchpilot:refresh-start'));
            
            // Poll every 5 seconds for up to 3 minutes
            let elapsed = 0;
            const pollInterval = setInterval(async () => {
                elapsed += 5000;
                await loadDashboard();
                
                if (elapsed >= 180000) {
                    clearInterval(pollInterval);
                   btn.disabled = false;
                   btn.innerHTML = '<span class="btn-icon">🔄</span> Refresh Status';
                   showStatus('Dashboard refreshed', 'success');
                   document.dispatchEvent(new CustomEvent('patchpilot:refresh-done'));
                }
            }, 5000);
        } else {
            throw new Error('Failed to trigger check');
        }
    } catch (error) {
        showStatus('Error triggering check: ' + error.message, 'error');
        btn.disabled = false;
        btn.innerHTML = '<span class="btn-icon">🔄</span> Refresh Status';
    }
}

// Handle Host Checkbox
function handleHostCheckbox(hostname) {
    if (selectedHosts.has(hostname)) {
        selectedHosts.delete(hostname);
    } else {
        selectedHosts.add(hostname);
    }
    updateSelectedCount();
    renderHostsTable();
}

// Handle Select All
function handleSelectAll() {
    const selectAllCheckbox = document.getElementById('select-all');
    
    if (selectAllCheckbox.checked) {
        hostsData.forEach(host => selectedHosts.add(host.hostname));
    } else {
        selectedHosts.clear();
    }
    
    updateSelectedCount();
    renderHostsTable();
}

// Update Selected Count
function updateSelectedCount() {
    const count = selectedHosts.size;
    document.getElementById('selected-count').textContent = count;
    document.getElementById('patch-selected-btn').disabled = count === 0;
}

// Handle Patch Selected
function handlePatchSelected() {
    if (selectedHosts.size === 0) return;
    
    // Clear any stale single-host selection from "Patch This Host"
    window.selectedHostsForPatch = Array.from(selectedHosts);
    
    const hostsList = document.getElementById('patch-hosts-list');
    hostsList.innerHTML = window.selectedHostsForPatch
        .map(hostname => `<li><strong>${hostname}</strong></li>`)
        .join('');
    
    document.getElementById('patch-modal').style.display = 'flex';
}

// Confirm Patch
async function confirmPatch() {
    const password = document.getElementById('become-password').value;
    
    if (!password) {
        alert('Please enter sudo password');
        return;
    }
    
    // Build the definitive hosts list
    const hostsToPatc = window.selectedHostsForPatch || Array.from(selectedHosts);
    
    if (hostsToPatc.length === 0) {
        alert('No hosts selected for patching');
        return;
    }
    
    // Warn about control node (but allow patching)
    const controlNodes = hostsToPatc.filter(hostname => {
        const host = hostsData.find(h => h.hostname === hostname);
        return host && host.is_control_node;
    });
    
    if (controlNodes.length > 0) {
        const proceed = confirm(
            `⚠️ NOTICE: You are patching the CONTROL NODE (${controlNodes.join(', ')}).\n\n` +
            `This host runs PatchPilot. If a reboot is required after patching, ` +
            `you'll need to manually reboot it to avoid service disruption.\n\n` +
            `Continue with patching?`
        );
        if (!proceed) {
            return;
        }
    }
    
    // 1. Close confirm modal
    closePatchModal();
    
    // 2. Show progress modal IMMEDIATELY — never wait on WebSocket
    showPatchProgressModal();
    const hostsDiv = document.getElementById('patch-progress-hosts');
    if (hostsDiv) {
        hostsDiv.innerHTML = hostsToPatc.map(h => 
            `<span id="patch-host-${h.replace(/[^a-zA-Z0-9]/g, '_')}" 
                   style="display:inline-flex;align-items:center;gap:6px;padding:6px 12px;border-radius:6px;font-size:13px;font-weight:600;background:var(--bg-dark);border:1px solid var(--border);color:var(--text-muted);">
                <span class="host-status-icon">⏳</span> ${h}
            </span>`
        ).join('');
    }
    const progressDiv = document.getElementById('patch-progress-messages');
    if (progressDiv) {
        const ts = new Date().toLocaleTimeString();
        const msgEl = document.createElement('div');
        msgEl.className = 'progress-message msg-start';
        msgEl.textContent = `[${ts}] Starting patch for ${hostsToPatc.length} host(s)...`;
        progressDiv.appendChild(msgEl);
    }
    
    // 3. Connect WebSocket in background — enhances the modal but doesn't block it
    connectPatchProgressWebSocket();
    
    // Fire activity bar event
    document.dispatchEvent(new CustomEvent('patchpilot:patch-start', { detail: { hosts: hostsToPatc } }));
    
    const btn = document.getElementById('patch-selected-btn');
    btn.disabled = true;
    btn.innerHTML = '<span class="btn-icon">⏳</span> Patching...';
    
    try {
        // 4. Fire patch request
        const response = await fetch(`${API_BASE_URL}/patch`, {
            method: 'POST',
            credentials: 'include',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                hostnames: hostsToPatc,
                become_password: password
            })
        });
        
        if (response.ok) {
            showStatus(
                `Patch operation initiated for ${hostsToPatc.length} host(s). This may take several minutes...`,
                'success'
            );
            document.dispatchEvent(new CustomEvent('patchpilot:patch-done', { detail: { success: true } }));
            
            // Start auto-refresh polling for all hosts
            hostsToPatc.forEach(hostname => pollForStatusChange(hostname));
          
            // Clear password field
            document.getElementById('become-password').value = '';
            
            // Clear selections
            selectedHosts.clear();
            updateSelectedCount();
            renderHostsTable();
            
            // Reload dashboard after 2 minutes
            setTimeout(loadDashboard, 2 * 60 * 1000);
        } else {
            throw new Error('Failed to trigger patch');
        }
    } catch (error) {
        showStatus('Error triggering patch: ' + error.message, 'error');
    } finally {
        // Clean up stale state
        window.selectedHostsForPatch = null;
        
        btn.disabled = false;
        btn.innerHTML = '<span class="btn-icon">⚡</span> Patch Selected (<span id="selected-count">0</span>)';
        updateSelectedCount();
    }
}

// Show Host Details
async function showHostDetails(hostname) {
    const modal = document.getElementById('host-modal');
    const loading = document.getElementById('host-details-loading');
    const content = document.getElementById('host-details-content');
    
    document.getElementById('modal-hostname').textContent = hostname;
    loading.style.display = 'block';
    content.style.display = 'none';
    modal.style.display = 'flex';
    
    try {
        // Get host details
        const hostResponse = await fetch(`${API_BASE_URL}/hosts/${hostname}`);
        const host = await hostResponse.json();
        
        // Get packages
        const packagesResponse = await fetch(`${API_BASE_URL}/hosts/${hostname}/packages`);
        const packages = await packagesResponse.json();
        
        // Populate modal
        document.getElementById('detail-ip').textContent = host.ip_address || 'N/A';
        document.getElementById('detail-os').textContent = 
            `${host.os_family || 'Unknown'} ${host.os_type ? `(${host.os_type})` : ''}`;
        
        const statusBadge = document.getElementById('detail-status');
        statusBadge.textContent = host.status;
        statusBadge.className = `status-badge status-${getStatusClass(host.status)}`;
        
        document.getElementById('detail-last-checked').textContent = 
            host.last_checked ? new Date(host.last_checked).toLocaleString() : 'Never';

        // Reboot status and auto-reboot setting
        const rebootStatus = document.getElementById('detail-reboot-status');
        if (rebootStatus) {
            rebootStatus.textContent = host.reboot_required ? '⚠️ Yes' : '✅ No';
            rebootStatus.style.color = host.reboot_required ? '#f59e0b' : '#10b981';
        }
        
        const autoRebootCheckbox = document.getElementById('detail-auto-reboot');
        if (autoRebootCheckbox) {
            const canWrite = isAuthenticated && currentUser && currentUser.role !== 'viewer';
            autoRebootCheckbox.checked = host.allow_auto_reboot || false;
            autoRebootCheckbox.disabled = !canWrite;
            autoRebootCheckbox.onclick = async () => {
                if (!canWrite) return;
                await updateAutoReboot(hostname, autoRebootCheckbox.checked);
            };
        }
        
        // Render packages
        const packagesList = document.getElementById('packages-list');
        if (packages.length === 0) {
            packagesList.innerHTML = '<p>No pending updates</p>';
        } else {
            packagesList.innerHTML = packages.map(pkg => `
                <div class="package-item">
                    <div>
                        <div class="package-name">${pkg.package_name}</div>
                        <div class="package-version">
                            ${pkg.current_version || 'N/A'} → ${pkg.available_version || 'N/A'}
                        </div>
                    </div>
                    <span class="status-badge status-updates-available">
                        ${pkg.update_type || 'update'}
                    </span>
                </div>
            `).join('');
        }
        
        loading.style.display = 'none';
        content.style.display = 'block';
         // Show/hide patch button based on status AND auth AND write access
        const patchBtn = document.getElementById('patch-host-btn');
        const canPatch = isAuthenticated && currentUser && currentUser.role !== 'viewer';
        if (host.status === 'updates-available' && canPatch) {
            patchBtn.style.display = 'inline-block';
        } else {
            patchBtn.style.display = 'none';
        }
    } catch (error) {
        loading.innerHTML = '<p style="color: #ef4444;">Error loading host details: ' + error.message + '</p>';
    }
}

// Close Host Modal
function closeHostModal() {
    document.getElementById('host-modal').style.display = 'none';
}

// Close Patch Modal
function closePatchModal() {
    document.getElementById('patch-modal').style.display = 'none';
}

// Patch Progress Modal Controls
function showPatchProgressModal() {
    const modal = document.getElementById('patch-progress-modal');
    const messagesDiv = document.getElementById('patch-progress-messages');
    const hostsDiv = document.getElementById('patch-progress-hosts');
    messagesDiv.innerHTML = ''; // Clear previous messages
    if (hostsDiv) hostsDiv.innerHTML = ''; // Clear previous host badges
    modal.style.display = 'flex';
}

function closePatchProgressModal() {
    const modal = document.getElementById('patch-progress-modal');
    modal.style.display = 'none';
    // Cancel any polling fallback that may be running
    if (_patchStatusPollTimer) {
        clearInterval(_patchStatusPollTimer);
        _patchStatusPollTimer = null;
    }
}

// Update auto-reboot setting for a host
async function updateAutoReboot(hostname, allowAutoReboot) {
    try {
        const host = hostsData.find(h => h.hostname === hostname);
        if (!host) return;
        
        const response = await fetch(`${API_BASE_URL}/settings/hosts/${host.id}`, {
            method: 'PUT',
            credentials: 'include',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                allow_auto_reboot: allowAutoReboot
            })
        });
        
        if (response.ok) {
            showStatus(`Auto-reboot ${allowAutoReboot ? 'enabled' : 'disabled'} for ${hostname}`, 'success');
        } else {
            throw new Error('Failed to update setting');
        }
    } catch (error) {
        showStatus('Error updating auto-reboot setting: ' + error.message, 'error');
    }
}

// Show Status Message
function showStatus(message, type = 'info') {
    const statusDiv = document.getElementById('status-message');
    statusDiv.textContent = message;
    statusDiv.className = `status-message ${type}`;
    statusDiv.style.display = 'flex';
    
    // Auto-hide after 5 seconds
    setTimeout(() => {
        statusDiv.style.display = 'none';
    }, 5000);
}

// Close modals on background click
window.onclick = function(event) {
    const hostModal = document.getElementById('host-modal');
    const patchModal = document.getElementById('patch-modal');
    const historyModal = document.getElementById('patch-history-modal');
    const packagesModal = document.getElementById('packages-modal');
    const alertsModal = document.getElementById('alerts-modal');
    
    if (event.target === hostModal) closeHostModal();
    if (event.target === patchModal) closePatchModal();
    if (event.target === historyModal) historyModal.style.display = 'none';
    if (event.target === packagesModal) packagesModal.style.display = 'none';
    if (event.target === alertsModal) alertsModal.style.display = 'none';
}
async function patchSingleHost() {
    const hostname = document.getElementById('modal-hostname').textContent;
    const statusBadge = document.getElementById('detail-status');
    const status = statusBadge.textContent;
    
    // Don't allow patching if up-to-date or unreachable
    if (status === 'UP-TO-DATE') {
        alert('This host is already up-to-date!');
        return;
    }
    
    if (status === 'UNREACHABLE') {
        alert('This host is unreachable and cannot be patched!');
        return;
    }
    
    // Close the host details modal
    closeHostModal();
    
    // Open the patch confirmation modal with this host
    const patchModal = document.getElementById('patch-modal');
    const hostsList = document.getElementById('patch-hosts-list');
    hostsList.innerHTML = `<li>${hostname}</li>`;
    
    // Store the hostname for the confirmPatch function
    window.selectedHostsForPatch = [hostname];
    
    patchModal.style.display = 'flex';
}
async function pollForStatusChange(hostname) {
    let attempts = 0;
    const maxAttempts = 20; // 20 attempts = 100 seconds max
    
    const checkStatus = async () => {
        attempts++;
        await loadHosts();
        
        if (attempts < maxAttempts) {
            setTimeout(checkStatus, 5000); // Check every 5 seconds
        }
    };
    
    setTimeout(checkStatus, 5000); // Start checking after 5 seconds
}

// =========================================================================
// DASHBOARD CHARTS
// =========================================================================

const CHART_COLORS = ['#3498db', '#f39c12', '#2ecc71', '#e74c3c', '#9b59b6', '#00c0ef', '#ff7799', '#ffaa77'];

async function loadChartData() {
    try {
        const response = await fetch(`${API_BASE_URL}/stats/charts?t=${Date.now()}${_ownerParam('&')}`, {
            cache: 'no-store'
        });
        const data = await response.json();

        // Build a stable OS → color map from the same order the donut chart uses,
        // so patch activity bars share exactly the same colors as the OS Distribution legend.
        const osColorMap = {};
        (data.os_distribution || []).forEach((item, i) => {
            osColorMap[item.os] = CHART_COLORS[i % CHART_COLORS.length];
        });

        renderPatchActivity(data.patch_activity || [], osColorMap, data.os_distribution || []);
        renderDonut('os-distribution-chart', data.os_distribution || [], 'os', 'Hosts');
        renderDonut('update-types-chart', data.update_types || [], 'type', 'Packages');
    } catch (error) {
        console.error('Error loading chart data:', error);
    }
}

function renderPatchActivity(activity, osColorMap, osDistribution) {
    const container = document.getElementById('patch-activity-chart');
    if (!container) return;

    osColorMap = osColorMap || {};

    const totalPatched = activity.reduce((s, d) => s + (d.patched || 0), 0);
    const totalFailed  = activity.reduce((s, d) => s + (d.failed  || 0), 0);
    const hasAny = totalPatched + totalFailed > 0;

    if (!hasAny) {
        container.innerHTML = '<div class="chart-empty">No patch activity recorded yet. Run your first patch to see data here.</div>';
        return;
    }

    // Collect all OS families that appear in the activity data (in donut order)
    const osOrder = (osDistribution || []).map(d => d.os);
    const activeOSes = new Set();
    activity.forEach(d => Object.keys(d.by_os || {}).forEach(os => activeOSes.add(os)));
    // Any OS not in donut order goes at the end
    const allOSes = [...osOrder.filter(o => activeOSes.has(o)),
                     ...[...activeOSes].filter(o => !osOrder.includes(o))];

    // Fallback color for "failed" bucket
    const FAILED_COLOR = '#e74c3c';

    const maxVal = Math.max(...activity.map(d => (d.patched || 0) + (d.failed || 0)), 1);
    const BAR_MAX_H = 100; // px — leave headroom for count labels

    const barsHTML = activity.map(d => {
        const patched = d.patched || 0;
        const failed  = d.failed  || 0;
        const total   = patched + failed;
        const byOs    = d.by_os  || {};
        const dayLabel = new Date(d.day + 'T12:00:00').toLocaleDateString('en-US', { month: 'short', day: 'numeric' });

        const countLabel = total > 0
            ? `<div class="chart-bar-count" title="${patched} patched, ${failed} failed">${total}</div>`
            : `<div class="chart-bar-count chart-bar-count--empty"></div>`;

        // Stack OS segments bottom-to-top (CSS column-reverse)
        let segmentsHTML = '';

        if (total > 0) {
            // Failed segment (always red, on top visually = last in DOM with column-reverse)
            if (failed > 0) {
                const h = Math.max((failed / maxVal) * BAR_MAX_H, 4);
                segmentsHTML += `<div class="chart-bar-seg" style="height:${h}px;background:${FAILED_COLOR}" title="${failed} failed"></div>`;
            }
            // Per-OS success segments
            allOSes.forEach(os => {
                const count = byOs[os] || 0;
                if (count === 0) return;
                const color = osColorMap[os] || CHART_COLORS[osOrder.indexOf(os) % CHART_COLORS.length] || '#aaaaaa';
                const h = Math.max((count / maxVal) * BAR_MAX_H, 4);
                segmentsHTML += `<div class="chart-bar-seg" style="height:${h}px;background:${color}" title="${count} patched (${os})"></div>`;
            });
            // Any patched not attributed to a known OS
            const osTotal = Object.values(byOs).reduce((a, b) => a + b, 0);
            const unattributed = patched - osTotal;
            if (unattributed > 0) {
                const h = Math.max((unattributed / maxVal) * BAR_MAX_H, 4);
                segmentsHTML += `<div class="chart-bar-seg" style="height:${h}px;background:#aaaaaa" title="${unattributed} patched (Unknown)"></div>`;
            }
        }

        return `
            <div class="chart-bar-group">
                ${countLabel}
                <div class="chart-bar-stack">${segmentsHTML}</div>
                <div class="chart-bar-label">${dayLabel}</div>
            </div>`;
    }).join('');

    // Legend: OS colors + failed, only show OSes that actually appear
    const legendItems = allOSes.map(os => {
        const color = osColorMap[os] || '#aaaaaa';
        return `<span class="activity-legend-item"><span class="activity-legend-swatch" style="background:${color}"></span>${os}</span>`;
    });
    if (totalFailed > 0) {
        legendItems.push(`<span class="activity-legend-item"><span class="activity-legend-swatch" style="background:${FAILED_COLOR}"></span>Failed</span>`);
    }

    // Summary strip
    const summaryHTML = `
        <div class="chart-activity-summary">
            <span class="summary-patched">✅ ${totalPatched} patched</span>
            ${totalFailed > 0 ? `<span class="summary-failed">❌ ${totalFailed} failed</span>` : ''}
            <span class="summary-window">— last 7 days</span>
            <span class="activity-legend">${legendItems.join('')}</span>
        </div>`;

    container.innerHTML = barsHTML + summaryHTML;
}

function renderDonut(elementId, items, keyField, label) {
    const container = document.getElementById(elementId);
    if (!container) return;
    
    if (items.length === 0) {
        container.innerHTML = `<div class="chart-empty">No data available</div>`;
        return;
    }
    
    const total = items.reduce((sum, item) => sum + item.count, 0);
    
    // Build conic-gradient segments
    let gradientParts = [];
    let angle = 0;
    items.forEach((item, i) => {
        const color = CHART_COLORS[i % CHART_COLORS.length];
        const slice = (item.count / total) * 360;
        gradientParts.push(`${color} ${angle}deg ${angle + slice}deg`);
        angle += slice;
    });
    
    // Build legend
    const legendHTML = items.map((item, i) => {
        const color = CHART_COLORS[i % CHART_COLORS.length];
        const name = item[keyField] || 'Unknown';
        return `<div class="donut-legend-item">
            <div class="swatch" style="background:${color}"></div>
            ${name}
            <span class="count">${item.count}</span>
        </div>`;
    }).join('');
    
    container.innerHTML = `
        <div class="donut-chart" style="background: conic-gradient(${gradientParts.join(', ')});">
            <div class="donut-hole">
                <div class="donut-total">${total}</div>
                <div class="donut-label">${label}</div>
            </div>
        </div>
        <div class="donut-legend">${legendHTML}</div>`;
}

// =========================================================================
// USER PROFILE
// =========================================================================

function openUserProfileModal() {
    if (!isAuthenticated || !currentUser) return;
    
    document.getElementById('profile-username').textContent = currentUser.username;
    document.getElementById('profile-role').textContent = currentUser.role;
    document.getElementById('current-password').value = '';
    document.getElementById('new-password').value = '';
    document.getElementById('confirm-new-password').value = '';
    document.getElementById('profile-error').style.display = 'none';
    document.getElementById('profile-success').style.display = 'none';
    document.getElementById('user-profile-modal').style.display = 'flex';
}

function closeUserProfileModal() {
    document.getElementById('user-profile-modal').style.display = 'none';
}

async function changePassword() {
    const currentPw = document.getElementById('current-password').value;
    const newPw = document.getElementById('new-password').value;
    const confirmPw = document.getElementById('confirm-new-password').value;
    const errorDiv = document.getElementById('profile-error');
    const successDiv = document.getElementById('profile-success');
    
    errorDiv.style.display = 'none';
    successDiv.style.display = 'none';
    
    if (!currentPw || !newPw || !confirmPw) {
        errorDiv.textContent = 'All fields are required';
        errorDiv.style.display = 'block';
        return;
    }
    
    if (newPw.length < 8) {
        errorDiv.textContent = 'New password must be at least 8 characters';
        errorDiv.style.display = 'block';
        return;
    }
    
    if (newPw !== confirmPw) {
        errorDiv.textContent = 'New passwords do not match';
        errorDiv.style.display = 'block';
        return;
    }
    
    try {
        const res = await fetch(`${API_BASE_URL}/auth/change-password`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'include',
            body: JSON.stringify({
                current_password: currentPw,
                new_password: newPw
            })
        });
        
        const data = await res.json();
        
        if (!res.ok) {
            errorDiv.textContent = data.detail || 'Failed to change password';
            errorDiv.style.display = 'block';
            return;
        }
        
        successDiv.textContent = '✓ Password changed successfully';
        successDiv.style.display = 'block';
        document.getElementById('current-password').value = '';
        document.getElementById('new-password').value = '';
        document.getElementById('confirm-new-password').value = '';
    } catch (e) {
        errorDiv.textContent = 'Connection error';
        errorDiv.style.display = 'block';
    }
}

// =========================================================================
// PATCH HISTORY MODAL
// =========================================================================

async function showPatchHistoryModal() {
    const modal = document.getElementById('patch-history-modal');
    const loading = document.getElementById('patch-history-loading');
    const content = document.getElementById('patch-history-content');
    const empty = document.getElementById('patch-history-empty');
    const tbody = document.getElementById('patch-history-tbody');
    
    modal.style.display = 'flex';
    loading.style.display = 'block';
    content.style.display = 'none';
    empty.style.display = 'none';
    
    try {
        const res = await fetch(`${API_BASE_URL}/patch-history?limit=100${_ownerParam('&')}`);
        const history = await res.json();
        
        loading.style.display = 'none';
        
        if (!history.length) {
            empty.style.display = 'block';
            return;
        }
        
        tbody.innerHTML = history.map(h => {
            const status = h.status === 'success' ? '✅ success' : '❌ ' + h.status;
            const statusColor = h.status === 'success' ? 'var(--green-bright)' : 'var(--red)';
            const duration = h.execution_time ? `${parseFloat(h.execution_time).toFixed(1)}s` : '—';
            const date = h.created_at ? new Date(h.created_at).toLocaleString() : '—';
            return `<tr>
                <td><strong style="color:var(--text-primary)">${h.hostname || 'Unknown'}</strong></td>
                <td><span style="color:${statusColor};font-weight:600">${status}</span></td>
                <td>${h.packages_updated || 0}</td>
                <td style="font-family:monospace;font-size:12px">${duration}</td>
                <td style="font-family:monospace;font-size:11px;color:var(--text-muted)">${date}</td>
            </tr>`;
        }).join('');
        
        content.style.display = 'block';
    } catch (err) {
        loading.innerHTML = '<span style="color:var(--red)">Error loading patch history</span>';
    }
}

// =========================================================================
// PACKAGES MODAL
// =========================================================================

async function showPackagesModal() {
    const modal = document.getElementById('packages-modal');
    const loading = document.getElementById('packages-modal-loading');
    const content = document.getElementById('packages-modal-content');
    const empty = document.getElementById('packages-modal-empty');
    const tbody = document.getElementById('packages-modal-tbody');
    
    modal.style.display = 'flex';
    loading.style.display = 'block';
    content.style.display = 'none';
    empty.style.display = 'none';
    
    try {
        // Aggregate packages from all hosts we already have
        const res = await fetch(`${API_BASE_URL}/hosts`);
        const _hosts = await res.json();
        const hosts = Array.isArray(_hosts) ? _hosts : [];
        
        const allPackages = [];
        for (const host of hosts) {
            if (host.total_updates > 0) {
                try {
                    const pkgRes = await fetch(`${API_BASE_URL}/hosts/${host.hostname}/packages`);
                    const pkgs = await pkgRes.json();
                    pkgs.forEach(p => allPackages.push({ ...p, hostname: host.hostname }));
                } catch (_) {}
            }
        }
        
        loading.style.display = 'none';
        
        if (!allPackages.length) {
            empty.style.display = 'block';
            return;
        }
        
        tbody.innerHTML = allPackages.map(p => `
            <tr>
                <td><span style="font-family:monospace;font-size:12px;color:var(--text-muted)">${p.hostname}</span></td>
                <td><strong style="color:var(--text-primary)">${p.package_name}</strong></td>
                <td style="font-family:monospace;font-size:11px;color:var(--text-muted)">${p.current_version || '—'}</td>
                <td style="font-family:monospace;font-size:11px;color:var(--cyan)">${p.available_version || '—'}</td>
                <td><span class="status-badge status-updates-available">${p.update_type || 'update'}</span></td>
            </tr>
        `).join('');
        
        content.style.display = 'block';
    } catch (err) {
        loading.innerHTML = '<span style="color:var(--red)">Error loading packages</span>';
    }
}

// =========================================================================
// ALERTS MODAL
// =========================================================================

async function showAlertsModal() {
    const modal = document.getElementById('alerts-modal');
    const loading = document.getElementById('alerts-modal-loading');
    const list = document.getElementById('alerts-modal-list');
    const empty = document.getElementById('alerts-modal-empty');
    
    modal.style.display = 'flex';
    loading.style.display = 'block';
    list.style.display = 'none';
    empty.style.display = 'none';
    
    try {
        const res = await fetch(`${API_BASE_URL}/alerts${_ownerParam('?')}`);
        const alerts = await res.json();
        
        loading.style.display = 'none';
        
        if (!alerts.length) {
            empty.style.display = 'block';
            return;
        }
        
        list.innerHTML = alerts.map(a => {
            const icon = a.severity === 'error' ? '❌' : a.severity === 'info' ? 'ℹ️' : '⚠️';
            const color = a.severity === 'error' ? 'var(--red)' : a.severity === 'info' ? 'var(--blue, #3b82f6)' : 'var(--amber)';
            const border = a.severity === 'error' ? '#ef444430' : a.severity === 'info' ? '#3b82f630' : '#f59e0b30';
            const checked = a.last_checked ? new Date(a.last_checked).toLocaleString() : 'Never';
            const canWrite = isAuthenticated && currentUser && currentUser.role !== 'viewer';
            const dismissBtn = (a.type === 'reboot_required' && canWrite)
                ? `<button onclick="dismissRebootAlert('${a.hostname}')" style="
                        background:rgba(255,171,0,0.12);border:1px solid rgba(255,171,0,0.3);
                        color:var(--amber);border-radius:5px;padding:4px 10px;font-size:11px;
                        cursor:pointer;white-space:nowrap;" title="Mark host as rebooted / clear this alert">
                        ✓ Mark Rebooted</button>`
                : '';
            return `<div style="display:flex;align-items:center;gap:12px;padding:14px 16px;margin-bottom:8px;background:var(--bg-dark);border:1px solid ${border};border-left:3px solid ${color};border-radius:8px;">
                <span style="font-size:18px">${icon}</span>
                <div style="flex:1;">
                    <div style="font-weight:600;color:var(--text-primary);margin-bottom:2px;">${a.message}</div>
                    <div style="font-size:11px;color:var(--text-muted);font-family:monospace">Last checked: ${checked}</div>
                </div>
                ${dismissBtn}
            </div>`;
        }).join('');
        
        list.style.display = 'block';
    } catch (err) {
        loading.innerHTML = '<span style="color:var(--red)">Error loading alerts</span>';
    }
}

async function dismissRebootAlert(hostname) {
    try {
        const res = await fetch(`${API_BASE_URL}/hosts/${hostname}/dismiss-reboot`, {
            method: 'POST',
            credentials: 'include'
        });
        if (res.ok) {
            showAlertsModal();          // Refresh alert list
            loadSidebarStats();         // Refresh alert badge
        } else {
            const d = await res.json().catch(() => ({}));
            alert('Could not dismiss alert: ' + (d.detail || res.status));
        }
    } catch (e) {
        alert('Error dismissing alert: ' + e.message);
    }
}

// =========================================================================
// PANEL COLLAPSE/EXPAND
// =========================================================================

function togglePanel(panelId) {
    const panel = document.getElementById(panelId);
    if (!panel) return;
    panel.classList.toggle('collapsed');
    const btn = panel.querySelector('.panel-toggle');
    if (btn) btn.textContent = panel.classList.contains('collapsed') ? '▸' : '▾';
    // Save state to localStorage
    try {
        const states = JSON.parse(localStorage.getItem('patchpilot-panels') || '{}');
        states[panelId] = panel.classList.contains('collapsed');
        localStorage.setItem('patchpilot-panels', JSON.stringify(states));
    } catch(e) {}
}

// Restore collapsed panel states on load
(function restorePanelStates() {
    try {
        const states = JSON.parse(localStorage.getItem('patchpilot-panels') || '{}');
        Object.entries(states).forEach(([id, collapsed]) => {
            if (collapsed) {
                const panel = document.getElementById(id);
                if (panel) {
                    panel.classList.add('collapsed');
                    const btn = panel.querySelector('.panel-toggle');
                    if (btn) btn.textContent = '▸';
                }
            }
        });
    } catch(e) {}
})();

// ============================================================================
// ACTIVITY STATUS BAR
// Pillbox-style live feed of application events in the top bar.
// Polls schedule status and hooks into patch events.
// ============================================================================

const activityPills = new Map(); // key -> { el, timer }

function addActivityPill(key, label, variant = 'running', autoClearMs = 0) {
    const container = document.getElementById('activity-pills');
    if (!container) return;

    // Remove existing pill for same key
    removeActivityPill(key);

    const pill = document.createElement('div');
    pill.className = `activity-pill pill-${variant}`;
    pill.dataset.key = key;
    pill.innerHTML = `<span class="pill-dot"></span>${label}`;
    container.appendChild(pill);

    let timer = null;
    if (autoClearMs > 0) {
        timer = setTimeout(() => removeActivityPill(key), autoClearMs);
    }
    activityPills.set(key, { el: pill, timer });
}

function removeActivityPill(key) {
    const entry = activityPills.get(key);
    if (entry) {
        if (entry.timer) clearTimeout(entry.timer);
        entry.el.remove();
        activityPills.delete(key);
    }
}

function updateActivityPill(key, label, variant) {
    const entry = activityPills.get(key);
    if (entry) {
        entry.el.className = `activity-pill pill-${variant}`;
        entry.el.innerHTML = `<span class="pill-dot"></span>${label}`;
    } else {
        addActivityPill(key, label, variant);
    }
}

// Persistent last-seen status across poll cycles so we detect transitions
// even when a schedule starts AND finishes between two polls.
const _schedLastSeen = new Map(); // scheduleId -> { status, retry_count, name }

// Poll schedule status every 60 seconds (uses lightweight /active endpoint)
async function pollScheduleStatus() {
    try {
        const res = await fetch(`/api/schedules/active${_ownerParam('?')}`, { credentials: 'include' });
        if (!res.ok) return;
        const schedules = await res.json();

        const seenIds = new Set();

        for (const sched of schedules) {
            const key  = `sched-${sched.id}`;
            const prev = _schedLastSeen.get(sched.id) || {};
            const statusChanged = prev.status !== sched.last_status;
            seenIds.add(sched.id);
            _schedLastSeen.set(sched.id, { status: sched.last_status, retry_count: sched.retry_count, name: sched.name });

            if (sched.last_status === 'running') {
                addActivityPill(key, `⏱ Schedule "${sched.name}" running…`, 'running');

            } else if (sched.last_status === 'partial') {
                const n = sched.retry_count || '?';
                addActivityPill(key,
                    `↩ Schedule "${sched.name}" — retrying ${n} host${n === 1 ? '' : 's'}…`,
                    'warning');

            } else if (sched.last_status === 'success') {
                // Always remove the running pill
                removeActivityPill(key);
                // Show done pill on any transition from a previous status
                // (prev.status guard prevents spurious pill on initial page load)
                if (statusChanged && prev.status) {
                    const doneKey = key + '-done';
                    removeActivityPill(doneKey);
                    addActivityPill(doneKey, `✓ Schedule "${sched.name}" completed`, 'success', 14000);
                }

            } else if (sched.last_status === 'error') {
                removeActivityPill(key);
                if (statusChanged && prev.status) {
                    const errKey = key + '-err';
                    removeActivityPill(errKey);
                    addActivityPill(errKey, `✗ Schedule "${sched.name}" failed`, 'error', 30000);
                }
            }
        }

        // Handle schedules that dropped off the active list
        for (const [key] of activityPills) {
            if (key.startsWith('sched-') && !key.endsWith('-done') && !key.endsWith('-err')) {
                const id = key.replace('sched-', '');
                if (!seenIds.has(id)) {
                    // If it was running when we last saw it, show a completion pill
                    const prev = _schedLastSeen.get(id);
                    if (prev && prev.status === 'running' && prev.name) {
                        const doneKey = `sched-${id}-done`;
                        removeActivityPill(doneKey);
                        addActivityPill(doneKey, `✓ Schedule "${prev.name}" completed`, 'success', 14000);
                    }
                    removeActivityPill(key);
                }
            }
        }

        // Forget IDs we're no longer tracking
        for (const [id] of _schedLastSeen) {
            if (!seenIds.has(id)) _schedLastSeen.delete(id);
        }
    } catch (e) {
        // Silently ignore — not critical
    }
}

// Hook into patch operations to show pills
const _origShowStatus = window.showStatus;
// Override patch trigger to add activity pill
const _origConfirmPatch = window.confirmPatch;

// Expose helpers so inline event handlers can call addActivityPill
window.addActivityPill = addActivityPill;
window.removeActivityPill = removeActivityPill;
window.updateActivityPill = updateActivityPill;

// Patch progress pill via WebSocket messages
document.addEventListener('patchpilot:patch-start', (e) => {
    const hosts = e.detail?.hosts || [];
    const label = hosts.length === 1
        ? `🔧 Patching ${hosts[0]}…`
        : `🔧 Patching ${hosts.length} hosts…`;
    addActivityPill('patch-op', label, 'running');
});
document.addEventListener('patchpilot:patch-done', (e) => {
    removeActivityPill('patch-op');
    const ok = e.detail?.success !== false;
    addActivityPill('patch-done', ok ? '✓ Patch completed' : '✗ Patch failed', ok ? 'success' : 'error', 12000);
});
document.addEventListener('patchpilot:refresh-start', () => {
    addActivityPill('refresh-op', '🔄 Checking for updates…', 'running');
});
document.addEventListener('patchpilot:refresh-done', () => {
    removeActivityPill('refresh-op');
});

// Start polling
setInterval(pollScheduleStatus, 60000);
// Initial poll after a short delay (wait for auth)
setTimeout(pollScheduleStatus, 2000);

// =========================================================================
// SIDEBAR BADGE COUNTS (SSH Keys, Users, Schedules)
// =========================================================================

async function fetchSidebarCounts() {
    try {
        // SSH Keys count
        const sshRes = await fetch(`${API_BASE_URL}/settings/ssh-keys`, { credentials: 'include' });
        if (sshRes.ok) {
            const keys = await sshRes.json();
            const badge = document.getElementById('sidebar-sshkeys-badge');
            if (badge && keys.length > 0) {
                badge.textContent = keys.length;
                badge.style.display = 'inline-flex';
            }
        }
    } catch(e) { /* not critical */ }

    try {
        // Schedules count
        const schRes = await fetch(`${API_BASE_URL}/schedules`, { credentials: 'include' });
        if (schRes.ok) {
            const schedules = await schRes.json();
            const badge = document.getElementById('sidebar-schedules-badge');
            if (badge && schedules.length > 0) {
                badge.textContent = schedules.length;
                badge.style.display = 'inline-flex';
            }
        }
    } catch(e) { /* not critical */ }

    try {
        // Users count (admin only - will 401 for non-admins, that's fine)
        const usrRes = await fetch(`${API_BASE_URL}/auth/users`, { credentials: 'include' });
        if (usrRes.ok) {
            const users = await usrRes.json();
            const badge = document.getElementById('sidebar-users-badge');
            if (badge && users.length > 0) {
                badge.textContent = users.length;
                badge.style.display = 'inline-flex';
            }
        }
    } catch(e) { /* not critical */ }
}

// Run badge count fetch after auth check completes (delay to ensure auth state is ready)
setTimeout(fetchSidebarCounts, 3000);


// =========================================================================
// UPDATE BADGE — sidebar notification for available updates
// =========================================================================

async function checkForUpdateBadge() {
    try {
        const res = await fetch(`${API_BASE_URL}/updates/status`, { credentials: 'include' });
        if (!res.ok) return;
        const data = await res.json();
        const badge = document.getElementById('sidebar-update-badge');
        if (!badge) return;
        if (data.update_available) {
            badge.style.display = 'flex';
            badge.title = `v${data.current_version} → v${data.latest_version}`;
        } else {
            badge.style.display = 'none';
        }
    } catch (_) {
        // Silently ignore — update check is non-critical
    }
}

// =========================================================================
// CONSOLE STATUS BAR
// =========================================================================

(function() {
    const CONSOLE_MAX_LINES = 300;
    const CONSOLE_STATE_KEY = 'patchpilot-console-expanded';
    const CONSOLE_TAB_KEY   = 'patchpilot-console-tab';
    // Restore persisted state (default: collapsed, backend tab)
    let consoleExpanded = localStorage.getItem(CONSOLE_STATE_KEY) === 'true';
    let activeTab = localStorage.getItem(CONSOLE_TAB_KEY) || 'backend';
    let allLines = []; // { source, level, time, msg }
    let errCount = 0;
    let warnCount = 0;
    let backendPollTimer = null;
    let lastBackendTs = null;

    // ── Intercept native console methods ──────────────────────────────────
    const origLog   = console.log.bind(console);
    const origWarn  = console.warn.bind(console);
    const origError = console.error.bind(console);
    const origDebug = console.debug.bind(console);

    function captureConsole(level, args) {
        const msg = args.map(a => (typeof a === 'object' ? JSON.stringify(a) : String(a))).join(' ');
        addLine('fe', level, msg);
    }

    console.log   = (...a) => { origLog(...a);   captureConsole('info',  a); };
    console.warn  = (...a) => { origWarn(...a);  captureConsole('warn',  a); };
    console.error = (...a) => { origError(...a); captureConsole('error', a); };
    console.debug = (...a) => { origDebug(...a); captureConsole('debug', a); };

    window.addEventListener('error', (e) => {
        addLine('fe', 'error', `Uncaught: ${e.message} (${e.filename}:${e.lineno})`);
    });

    // ── Line management ────────────────────────────────────────────────────
    function addLine(source, level, msg) {
        const now = new Date();
        const ts  = now.toTimeString().slice(0, 8);
        const entry = { source, level, ts, msg, epoch: now.getTime() };
        allLines.push(entry);
        if (allLines.length > CONSOLE_MAX_LINES) allLines.shift();

        if (level === 'error') { errCount++; updateCounts(); }
        if (level === 'warn')  { warnCount++; updateCounts(); }

        if (activeTab === source || (activeTab === 'frontend' && source === 'fe') || (activeTab === 'backend' && source === 'be')) {
            appendLineToDOM(entry);
        }
    }

    function appendLineToDOM(entry) {
        const out = document.getElementById('console-output');
        if (!out) return;
        const div = document.createElement('div');
        div.className = `console-line console-line--${entry.level}`;
        div.innerHTML = `<span class="console-line__time">${entry.ts}</span>` +
            `<span class="console-line__source console-line__source--${entry.source}">${entry.source === 'fe' ? 'FE' : 'BE'}</span>` +
            `<span class="console-line__msg">${escapeConsoleLine(entry.msg)}</span>`;
        out.appendChild(div);
        // Auto-scroll if near bottom
        if (out.scrollHeight - out.scrollTop - out.clientHeight < 60) {
            out.scrollTop = out.scrollHeight;
        }
    }

    function escapeConsoleLine(str) {
        return str
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');
    }

    function rebuildOutput() {
        const out = document.getElementById('console-output');
        if (!out) return;
        out.innerHTML = '';
        const srcFilter = activeTab === 'frontend' ? 'fe' : 'be';
        allLines.filter(l => l.source === srcFilter).forEach(l => appendLineToDOM(l));
        out.scrollTop = out.scrollHeight;
    }

    function updateCounts() {
        const eEl = document.getElementById('console-err-count');
        const wEl = document.getElementById('console-warn-count');
        if (eEl) { eEl.textContent = errCount + ' ERR';  eEl.style.display = errCount  > 0 ? 'inline' : 'none'; }
        if (wEl) { wEl.textContent = warnCount + ' WARN'; wEl.style.display = warnCount > 0 ? 'inline' : 'none'; }
    }

    // ── Backend log polling ────────────────────────────────────────────────
    async function pollBackendLogs() {
        try {
            const res = await fetch(`${API_BASE_URL}/backend-logs?limit=100`);
            if (!res.ok) return;
            const entries = await res.json();
            entries.forEach(e => {
                // Deduplicate: only add entries newer than lastBackendTs
                if (lastBackendTs && e.ts <= lastBackendTs) return;
                addLine('be', e.lvl || 'info', `[${e.name || 'app'}] ${e.msg}`);
            });
            if (entries.length > 0) lastBackendTs = entries[entries.length - 1].ts;
        } catch(e) { /* silent */ }
    }

    // ── Public control functions ───────────────────────────────────────────
    function applyConsoleState() {
        const bar = document.getElementById('console-bar');
        const btn = document.getElementById('console-toggle-btn');
        if (!bar) return;
        bar.classList.toggle('console-bar--collapsed', !consoleExpanded);
        bar.classList.toggle('console-bar--expanded',  consoleExpanded);
        if (btn) btn.textContent = consoleExpanded ? '▼' : '▲';
        // Adjust main content padding so console doesn't overlap content
        const main = document.querySelector('.main');
        if (main) main.style.paddingBottom = consoleExpanded ? '244px' : '52px';
    }

    window.toggleConsoleBar = function() {
        const bar = document.getElementById('console-bar');
        const btn = document.getElementById('console-toggle-btn');
        consoleExpanded = !consoleExpanded;
        localStorage.setItem(CONSOLE_STATE_KEY, consoleExpanded);
        applyConsoleState();
        if (consoleExpanded) {
            rebuildOutput();
            // Start backend polling when open
            if (!backendPollTimer) {
                pollBackendLogs();
                backendPollTimer = setInterval(pollBackendLogs, 5000);
            }
        } else {
            if (backendPollTimer) { clearInterval(backendPollTimer); backendPollTimer = null; }
        }
    };

    window.setConsoleTab = function(tab) {
        activeTab = tab;
        localStorage.setItem(CONSOLE_TAB_KEY, tab);
        document.getElementById('console-tab-frontend').classList.toggle('active', tab === 'frontend');
        document.getElementById('console-tab-backend').classList.toggle('active',  tab === 'backend');
        rebuildOutput();
        if (tab === 'backend' && !backendPollTimer && consoleExpanded) {
            pollBackendLogs();
            backendPollTimer = setInterval(pollBackendLogs, 5000);
        }
    };

    window.clearConsole = function() {
        const srcFilter = activeTab === 'frontend' ? 'fe' : 'be';
        allLines = allLines.filter(l => l.source !== srcFilter);
        if (srcFilter === 'fe') { errCount = 0; warnCount = 0; updateCounts(); }
        const out = document.getElementById('console-output');
        if (out) out.innerHTML = '';
    };

    // Set initial tab button state and restore persisted open/closed state
    document.addEventListener('DOMContentLoaded', () => {
        const fe = document.getElementById('console-tab-frontend');
        const be = document.getElementById('console-tab-backend');
        if (fe) fe.classList.toggle('active', activeTab === 'frontend');
        if (be) be.classList.toggle('active',  activeTab === 'backend');
        // Apply persisted expand/collapse state
        applyConsoleState();
        if (consoleExpanded) {
            rebuildOutput();
            pollBackendLogs();
            backendPollTimer = setInterval(pollBackendLogs, 5000);
        }
        if (activeTab === 'backend') {
            addLine('be', 'info', 'Console initialized — PatchPilot backend ready');
        } else {
            addLine('fe', 'info', 'Console initialized — PatchPilot frontend ready');
        }
    });
})();
