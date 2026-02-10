// API Configuration
const API_BASE_URL = window.location.hostname === 'localhost' 
    ? 'http://localhost:8000/api'
    : '/api';  // Use relative path in production

// WebSocket Configuration
const WS_BASE_URL = window.location.hostname === 'localhost'
    ? 'ws://localhost:8000'
    : `ws://${window.location.host}`;

// WebSocket for real-time patch progress
let patchProgressWS = null;

function connectPatchProgressWebSocket() {
    if (patchProgressWS) return; // Already connected
    
    patchProgressWS = new WebSocket(`${WS_BASE_URL}/ws/patch-progress`);
    
    patchProgressWS.onopen = () => {
        console.log('WebSocket connected');
    };
    
    patchProgressWS.onmessage = (event) => {
        const data = JSON.parse(event.data);
        handlePatchProgress(data);
    };
    
    patchProgressWS.onerror = (error) => {
        console.error('WebSocket error:', error);
    };
    
    patchProgressWS.onclose = () => {
        console.log('WebSocket closed');
        patchProgressWS = null;
    };
}

function handlePatchProgress(data) {
    const progressDiv = document.getElementById('patch-progress-messages');
    if (!progressDiv) return;
    
    const timestamp = new Date().toLocaleTimeString();
    let message = '';
    
    switch(data.type) {
        case 'start':
            message = `[${timestamp}] ${data.message}`;
            showPatchProgressModal();
            break;
        case 'progress':
            message = `[${timestamp}] ${data.message}`;
            break;
        case 'success':
            message = `[${timestamp}] ✅ ${data.message}`;
            break;
        case 'complete':
            message = `[${timestamp}] 🎉 ${data.message}`;
            setTimeout(() => {
                closePatchProgressModal();
                loadHosts(); // Refresh
            }, 2000);
            break;
        case 'error':
            message = `[${timestamp}] ❌ ${data.message}`;
            break;
    }
    
    if (message) {
        const msgEl = document.createElement('div');
        msgEl.className = 'progress-message';
        msgEl.textContent = message;
        progressDiv.appendChild(msgEl);
        progressDiv.scrollTop = progressDiv.scrollHeight;
    }
}

// State
let hostsData = [];
let selectedHosts = new Set();

// Initialize dashboard on load
document.addEventListener('DOMContentLoaded', () => {
    initializeEventListeners();
    loadDashboard();
    
    // Auto-refresh every 5 minutes
    setInterval(loadDashboard, 5 * 60 * 1000);
});

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
            loadHosts()
        ]);
    } catch (error) {
        showStatus('Error loading dashboard: ' + error.message, 'error');
    }
}

// Load Statistics
async function loadStats() {
    try {
        const response = await fetch(`${API_BASE_URL}/stats?t=${Date.now()}`, {
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
    } catch (error) {
        console.error('Error loading stats:', error);
    }
}

// Load Hosts
async function loadHosts() {
    try {
        const response = await fetch(`${API_BASE_URL}/hosts?t=${Date.now()}`, {
            cache: 'no-store',
            headers: {
                'Cache-Control': 'no-cache, no-store, must-revalidate',
                'Pragma': 'no-cache'
            }
        });
        hostsData = await response.json();
        
        renderHostsTable();
    } catch (error) {
        console.error('Error loading hosts:', error);
        showStatus('Error loading hosts: ' + error.message, 'error');
    }
}

// Render Hosts Table
function renderHostsTable() {
    const tbody = document.getElementById('hosts-table-body');
    
    if (hostsData.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" class="loading">No hosts found</td></tr>';
        return;
    }
    
    tbody.innerHTML = hostsData.map(host => {
        const isSelected = selectedHosts.has(host.hostname);
        const statusClass = getStatusClass(host.status);
        const lastChecked = host.last_checked 
            ? new Date(host.last_checked).toLocaleString()
            : 'Never';
        
        return `
            <tr class="${isSelected ? 'selected' : ''}" data-hostname="${host.hostname}">
                <td>
                    <input 
                        type="checkbox" 
                        class="host-checkbox" 
                        data-hostname="${host.hostname}"
                        ${isSelected ? 'checked' : ''}
                        onchange="handleHostCheckbox('${host.hostname}')"
                    />
                </td>
                <td>
                    <strong>${host.hostname}</strong>
                    ${host.is_control_node ? '<span class="control-node-badge">⚠️ CONTROL NODE</span>' : ''}
                </td>
                <td>${host.ip_address || 'N/A'}</td>
                <td>${host.os_family || 'Unknown'}</td>
                <td>
                    <span class="status-badge status-${statusClass}">
                        ${host.status}
                    </span>
                </td>
                <td>
                    ${host.total_updates > 0 
                        ? `<strong>${host.total_updates}</strong> update${host.total_updates > 1 ? 's' : ''}`
                        : '-'
                    }
                </td>
                <td>${lastChecked}</td>
                <td>
                    <button 
                        class="action-btn" 
                        onclick="showHostDetails('${host.hostname}')"
                    >
                        View Details
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
async function handleRefresh() {
    const btn = document.getElementById('refresh-btn');
    btn.disabled = true;
    btn.innerHTML = '<span class="btn-icon">⏳</span> Checking...';
    
    try {
        const response = await fetch(`${API_BASE_URL}/check`, {
            method: 'POST'
        });
        
        if (response.ok) {
            showStatus('Update check initiated. Waiting for completion...', 'success');
            
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
    
    const hostsList = document.getElementById('patch-hosts-list');
    hostsList.innerHTML = Array.from(selectedHosts)
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
    // NEW: Warn about control node (but allow patching)
    const hostsToPatc = window.selectedHostsForPatch || Array.from(selectedHosts);
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
    
    // Connect WebSocket for real-time progress
    connectPatchProgressWebSocket();
    
    closePatchModal();
    
    const btn = document.getElementById('patch-selected-btn');
    btn.disabled = true;
    btn.innerHTML = '<span class="btn-icon">⏳</span> Patching...';

    // Use window.selectedHostsForPatch if set (from "Patch This Host" button),
    // otherwise use selectedHosts (from checkboxes)
    
    try {
        const response = await fetch(`${API_BASE_URL}/patch`, {
            method: 'POST',
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
            
            // Start auto-refresh polling
            if (window.selectedHostsForPatch && window.selectedHostsForPatch.length > 0) {
                pollForStatusChange(window.selectedHostsForPatch[0]);
            } else if (hostsToPatc.length > 0) {
                pollForStatusChange(hostsToPatc[0]);
            }
          
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
        btn.disabled = false;
        btn.innerHTML = '<span class="btn-icon">⚡</span> Patch Selected (<span id="selected-count">0</span>)';
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
            autoRebootCheckbox.checked = host.allow_auto_reboot || false;
            autoRebootCheckbox.onclick = async () => {
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
         // Show/hide patch button based on status
        const patchBtn = document.getElementById('patch-host-btn');
        if (host.status === 'updates-available') {
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
    messagesDiv.innerHTML = ''; // Clear previous messages
    modal.style.display = 'flex';
}

function closePatchProgressModal() {
    const modal = document.getElementById('patch-progress-modal');
    modal.style.display = 'none';
}

// Update auto-reboot setting for a host
async function updateAutoReboot(hostname, allowAutoReboot) {
    try {
        const host = hostsData.find(h => h.hostname === hostname);
        if (!host) return;
        
        const response = await fetch(`${API_BASE_URL}/settings/hosts/${host.id}`, {
            method: 'PUT',
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
    
    if (event.target === hostModal) {
        closeHostModal();
    }
    if (event.target === patchModal) {
        closePatchModal();
    }
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
