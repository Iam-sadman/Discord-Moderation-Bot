/**
 * qc_faults.js — QC & Fault Tracker Client Engine
 * Implements:
 * - 64-Bit Discord Snowflake ID safety (strictly preserves string representation)
 * - Overview KPI rendering, project-wise and team-wise summary tables
 * - Searchable member picker dropdown with raw 18-19 digit fallback
 * - Filterable fault logs explorer with pagination, modal preview, and acknowledge actions
 * - Direct Web Fault Logging with Discord alert embed dispatch
 * - Excel (.xlsx) & CSV export triggers respecting active filters
 * - Dynamic Role-Mapped Teams management (10+ teams expandable anytime)
 * - Projects CRUD and status toggles
 * - Default alert channel & checker roles configuration
 */

let qcCurrentSubTab = 'overview';
let qcFaultsOffset = 0;
const qcFaultsLimit = 25;
let qcDebounceTimer = null;
let qcMemberSearchTimer = null;

// =============================================================================
// 1. Tab Initialization & Sub-Tab Navigation
// =============================================================================

function initQCTrackerTab() {
    switchQCSubTab(qcCurrentSubTab);
}

function switchQCSubTab(subTabName, btn = null) {
    qcCurrentSubTab = subTabName;

    // Update subnav buttons
    document.querySelectorAll('.qc-subnav-btn').forEach(b => b.classList.remove('active'));
    if (btn) {
        btn.classList.add('active');
    } else {
        const targetBtn = document.getElementById(`qc-subnav-btn-${subTabName}`);
        if (targetBtn) targetBtn.classList.add('active');
    }

    // Update sub-tab panels
    document.querySelectorAll('.qc-sub-panel').forEach(p => {
        p.classList.remove('active');
        p.style.display = 'none';
    });
    const activePanel = document.getElementById(`qc-panel-${subTabName}`);
    if (activePanel) {
        activePanel.classList.add('active');
        activePanel.style.display = 'block';
    }

    // Fetch relevant data
    if (subTabName === 'overview') {
        loadQCOverview();
    } else if (subTabName === 'faults') {
        loadQCFaults(0);
        populateQCFaultFilterDropdowns();
    } else if (subTabName === 'projects') {
        loadQCProjects();
    } else if (subTabName === 'teams') {
        loadQCTeams();
    } else if (subTabName === 'settings') {
        loadQCSettings();
    }
}

// =============================================================================
// 2. Overview & Summary Analytics
// =============================================================================

async function loadQCOverview() {
    const data = await apiRequest(`/api/guild/${guildId}/qc/overview`, 'GET');
    if (!data || !data.success) {
        return;
    }

    // Populate KPIs
    const k = data.kpis || {};
    document.getElementById('qc-kpi-total').textContent = k.total_faults ?? 0;
    document.getElementById('qc-kpi-critical').textContent = k.critical_faults ?? 0;
    document.getElementById('qc-kpi-major').textContent = k.major_faults ?? 0;
    document.getElementById('qc-kpi-minor').textContent = k.minor_faults ?? 0;
    document.getElementById('qc-kpi-open').textContent = k.open_faults ?? 0;
    document.getElementById('qc-kpi-ack').textContent = `${k.ack_faults ?? 0} (${k.ack_rate ?? 100}%)`;
    document.getElementById('qc-kpi-projects').textContent = k.active_projects ?? 0;
    document.getElementById('qc-kpi-teams').textContent = k.total_teams ?? 0;

    // Render Project-Wise Summary Table
    renderQCProjectsSummaryTable(data.projects || []);

    // Render Team-Wise Summary Table
    renderQCTeamsSummaryTable(data.teams || []);

    // Also populate labeler search initial state
    searchQCLabelers();
}

function renderQCProjectsSummaryTable(projects) {
    const tbody = document.getElementById('qc-projects-summary-tbody');
    if (!tbody) return;

    if (!projects || projects.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted py-4">No projects configured or logged yet.</td></tr>';
        return;
    }

    tbody.innerHTML = projects.map(p => {
        const topTypes = (p.top_fault_types && p.top_fault_types.length > 0)
            ? p.top_fault_types.map(t => `<span class="qc-type-tag">${escapeHTML(t)}</span>`).join(' ')
            : '<span class="text-muted text-xs">None</span>';

        return `
            <tr>
                <td>
                    <span class="font-bold text-main">📁 ${escapeHTML(p.project_code)}</span>
                    <div class="text-xs text-muted">${escapeHTML(p.project_name || '')}</div>
                </td>
                <td><span class="font-bold">${p.total_faults}</span></td>
                <td><span class="badge-sev badge-sev-minor">${p.minor_count}</span></td>
                <td><span class="badge-sev badge-sev-major">${p.major_count}</span></td>
                <td><span class="badge-sev badge-sev-critical">${p.critical_count}</span></td>
                <td>${topTypes}</td>
                <td>
                    <button class="btn btn-secondary btn-sm" onclick="filterFaultsByProject('${escapeHTML(p.project_code)}')" title="View faults for this project">
                        🔍 View Faults
                    </button>
                </td>
            </tr>
        `;
    }).join('');
}

function renderQCTeamsSummaryTable(teams) {
    const tbody = document.getElementById('qc-teams-summary-tbody');
    if (!tbody) return;

    if (!teams || teams.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted py-4">No teams configured yet. Configure teams in the Teams tab.</td></tr>';
        return;
    }

    tbody.innerHTML = teams.map(t => {
        const leaderHtml = t.leader_user_id
            ? `<div class="d-flex align-center gap-2">
                 ${t.leader_avatar ? `<img src="${t.leader_avatar}" class="qc-member-picker-avatar" alt="Avatar">` : ''}
                 <div>
                   <span class="font-semibold text-main">${escapeHTML(t.leader_display_name || t.leader_name || 'Leader')}</span>
                   <div class="text-xs text-muted">ID: ${escapeHTML(String(t.leader_user_id))}</div>
                 </div>
               </div>`
            : '<span class="text-muted italic">Unassigned</span>';

        return `
            <tr>
                <td>
                    <span class="font-bold text-main">👥 ${escapeHTML(t.team_name)}</span>
                </td>
                <td>
                    <span class="server-tag" style="border-left: 3px solid ${escapeHTML(t.role_color || '#6366f1')};">
                        🛡️ ${escapeHTML(t.role_name || 'Role')}
                    </span>
                </td>
                <td>${leaderHtml}</td>
                <td><span class="font-bold">${t.total_faults}</span></td>
                <td>
                    <span class="badge-sev badge-sev-minor" title="Minor">${t.minor_count}</span>
                    <span class="badge-sev badge-sev-major" title="Major">${t.major_count}</span>
                    <span class="badge-sev badge-sev-critical" title="Critical">${t.critical_count}</span>
                </td>
                <td>
                    <div class="qc-rate-bar-bg" title="${t.ack_rate}% acknowledged">
                        <div class="qc-rate-bar-fill" style="width: ${Math.min(100, t.ack_rate)}%;"></div>
                    </div>
                    <span class="text-xs font-semibold">${t.ack_rate}%</span>
                </td>
                <td>
                    <button class="btn btn-secondary btn-sm" onclick="filterFaultsByTeam('${escapeHTML(t.team_name)}')" title="View faults for this team">
                        🔍 View Faults
                    </button>
                </td>
            </tr>
        `;
    }).join('');
}

// =============================================================================
// 3. Labeler Search & Diagnostics
// =============================================================================

function debounceQCLabelerSearch() {
    clearTimeout(qcDebounceTimer);
    qcDebounceTimer = setTimeout(() => searchQCLabelers(), 300);
}

async function searchQCLabelers() {
    const input = document.getElementById('qc-labeler-search-input');
    const query = input ? input.value.trim() : '';

    const data = await apiRequest(`/api/guild/${guildId}/qc/labelers?search=${encodeURIComponent(query)}&limit=15`, 'GET');
    const container = document.getElementById('qc-labelers-search-results');
    if (!container) return;

    if (!data || !data.success || !data.labelers || data.labelers.length === 0) {
        container.innerHTML = '<div class="text-muted text-center py-4">No labelers found matching the search criteria.</div>';
        return;
    }

    container.innerHTML = `
        <div class="qc-table-responsive">
            <table class="qc-table">
                <thead>
                    <tr>
                        <th>Labeler</th>
                        <th>Team</th>
                        <th>Total Faults</th>
                        <th>Severity Breakdown</th>
                        <th>Fix / Ack Rate</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    ${data.labelers.map(l => `
                        <tr>
                            <td>
                                <span class="font-bold text-main">👤 ${escapeHTML(l.labeler_name)}</span>
                                <div class="text-xs text-muted">ID: ${escapeHTML(String(l.labeler_id))}</div>
                            </td>
                            <td><span class="text-muted">${escapeHTML(l.team_name || 'Unassigned')}</span></td>
                            <td><span class="font-bold text-main">${l.total_faults}</span></td>
                            <td>
                                <span class="badge-sev badge-sev-minor">${l.minor_count}m</span>
                                <span class="badge-sev badge-sev-major">${l.major_count}M</span>
                                <span class="badge-sev badge-sev-critical">${l.critical_count}C</span>
                            </td>
                            <td>
                                <div class="qc-rate-bar-bg">
                                    <div class="qc-rate-bar-fill" style="width: ${Math.min(100, l.ack_rate)}%;"></div>
                                </div>
                                <span class="text-xs font-semibold">${l.ack_rate}%</span>
                            </td>
                            <td>
                                <button class="btn btn-primary btn-sm" onclick="viewQCLabelerDetails('${escapeHTML(String(l.labeler_id))}')">
                                    📊 Diagnostic Profile
                                </button>
                            </td>
                        </tr>
                    `).join('')}
                </tbody>
            </table>
        </div>
    `;
}

async function viewQCLabelerDetails(labelerId) {
    const safeId = String(labelerId).trim();
    const data = await apiRequest(`/api/guild/${guildId}/qc/labeler/${encodeURIComponent(safeId)}`, 'GET');
    if (!data || !data.success || !data.details) {
        showToast(data?.error || 'Failed to load labeler profile', 'error');
        return;
    }

    const d = data.details;
    const m = data.member || {};
    const sum = d.summary || {};

    document.getElementById('qc-lp-name').textContent = m.display_name || sum.labeler_name || `User #${safeId}`;
    document.getElementById('qc-lp-id').textContent = safeId;
    document.getElementById('qc-lp-team').textContent = sum.team_name || 'Unassigned';

    const avatarEl = document.getElementById('qc-lp-avatar');
    if (avatarEl) {
        if (m.avatar) {
            avatarEl.src = m.avatar;
            avatarEl.style.display = 'block';
            document.getElementById('qc-lp-avatar-ph').style.display = 'none';
        } else {
            avatarEl.style.display = 'none';
            const ph = document.getElementById('qc-lp-avatar-ph');
            ph.textContent = (m.display_name || sum.labeler_name || 'U')[0].toUpperCase();
            ph.style.display = 'flex';
        }
    }

    // Stats
    document.getElementById('qc-lp-total').textContent = sum.total_faults ?? 0;
    document.getElementById('qc-lp-critical').textContent = sum.critical_count ?? 0;
    document.getElementById('qc-lp-major').textContent = sum.major_count ?? 0;
    document.getElementById('qc-lp-minor').textContent = sum.minor_count ?? 0;
    document.getElementById('qc-lp-ack-rate').textContent = `${sum.ack_rate ?? 100}%`;

    // Recurring fault types
    const recContainer = document.getElementById('qc-lp-recurring-types');
    if (recContainer) {
        if (d.recurring_types && d.recurring_types.length > 0) {
            recContainer.innerHTML = d.recurring_types.map(t =>
                `<span class="qc-type-tag">🏷️ ${escapeHTML(t.fault_type)} (${t.count})</span>`
            ).join(' ');
        } else {
            recContainer.innerHTML = '<span class="text-muted text-xs">No recurring categories tagged yet.</span>';
        }
    }

    // Projects breakdown
    const projContainer = document.getElementById('qc-lp-projects-tbody');
    if (projContainer) {
        if (d.projects_breakdown && d.projects_breakdown.length > 0) {
            projContainer.innerHTML = d.projects_breakdown.map(p => `
                <tr>
                    <td><strong>📁 ${escapeHTML(p.project_code)}</strong></td>
                    <td>${p.total}</td>
                    <td><span class="badge-sev badge-sev-minor">${p.minor}</span></td>
                    <td><span class="badge-sev badge-sev-major">${p.major}</span></td>
                    <td><span class="badge-sev badge-sev-critical">${p.critical}</span></td>
                </tr>
            `).join('');
        } else {
            projContainer.innerHTML = '<tr><td colspan="5" class="text-center text-muted">No project faults found.</td></tr>';
        }
    }

    // History Table with Clickable Discord CDN Screenshot links
    const histContainer = document.getElementById('qc-lp-history-tbody');
    if (histContainer) {
        if (d.history && d.history.length > 0) {
            histContainer.innerHTML = d.history.map(h => {
                const screenshotHtml = h.image_url
                    ? `<a href="javascript:void(0)" onclick="openQCScreenshotPreview('${escapeHTML(h.image_url)}')" class="qc-thumb-link">
                         🖼️ View Screenshot
                       </a>`
                    : '<span class="text-muted text-xs">None</span>';

                const statusBadge = (h.status === 'acknowledged')
                    ? '<span class="badge-status badge-status-ack">✅ Acknowledged</span>'
                    : '<span class="badge-status badge-status-open">🔴 Open</span>';

                return `
                    <tr>
                        <td>#${h.id}</td>
                        <td><span class="font-bold">📁 ${escapeHTML(h.project_code)}</span></td>
                        <td><span class="badge-sev badge-sev-${escapeHTML(h.severity)}">${escapeHTML(h.severity)}</span></td>
                        <td>${statusBadge}</td>
                        <td>${escapeHTML(h.fault_type || 'General')}</td>
                        <td class="text-xs" style="max-width: 250px;">${escapeHTML(h.description || '')}</td>
                        <td>${screenshotHtml}</td>
                        <td class="text-xs text-muted">${escapeHTML(h.created_at || '')}</td>
                    </tr>
                `;
            }).join('');
        } else {
            histContainer.innerHTML = '<tr><td colspan="8" class="text-center text-muted">No fault history recorded.</td></tr>';
        }
    }

    // Open modal
    openQCModal('modal-qc-labeler-details');
}

// =============================================================================
// 4. Fault Logs Explorer & Filtering
// =============================================================================

async function loadQCFaults(offset = 0) {
    qcFaultsOffset = Math.max(0, offset);

    const project = document.getElementById('qc-filter-project')?.value || '';
    const team = document.getElementById('qc-filter-team')?.value || '';
    const severity = document.getElementById('qc-filter-severity')?.value || '';
    const status = document.getElementById('qc-filter-status')?.value || '';
    const dateFrom = document.getElementById('qc-filter-date-from')?.value || '';
    const dateTo = document.getElementById('qc-filter-date-to')?.value || '';
    const search = document.getElementById('qc-filter-search')?.value || '';

    let url = `/api/guild/${guildId}/qc/faults?limit=${qcFaultsLimit}&offset=${qcFaultsOffset}`;
    if (project) url += `&project_code=${encodeURIComponent(project)}`;
    if (team) url += `&team_name=${encodeURIComponent(team)}`;
    if (severity && severity !== 'all') url += `&severity=${encodeURIComponent(severity)}`;
    if (status && status !== 'all') url += `&status=${encodeURIComponent(status)}`;
    if (dateFrom) url += `&date_from=${encodeURIComponent(dateFrom)}`;
    if (dateTo) url += `&date_to=${encodeURIComponent(dateTo)}`;
    if (search) url += `&search=${encodeURIComponent(search)}`;

    const tbody = document.getElementById('qc-faults-table-tbody');
    if (tbody) {
        tbody.innerHTML = '<tr><td colspan="10" class="text-center py-4">⏳ Loading fault logs...</td></tr>';
    }

    const data = await apiRequest(url, 'GET');
    if (!data || !data.success) {
        if (tbody) tbody.innerHTML = '<tr><td colspan="10" class="text-center text-danger py-4">Failed to load fault logs.</td></tr>';
        return;
    }

    renderQCFaultsTable(data.faults || [], data.total || 0);
}

function renderQCFaultsTable(faults, total) {
    const tbody = document.getElementById('qc-faults-table-tbody');
    if (!tbody) return;

    if (!faults || faults.length === 0) {
        tbody.innerHTML = '<tr><td colspan="10" class="text-center text-muted py-6">No fault logs match your current filters.</td></tr>';
        renderQCPagination(0, 0);
        return;
    }

    tbody.innerHTML = faults.map(f => {
        const sevClass = `badge-sev-${escapeHTML(f.severity || 'minor')}`;
        const isAck = (f.status === 'acknowledged');
        const statusBadge = isAck
            ? '<span class="badge-status badge-status-ack">✅ Acknowledged</span>'
            : '<span class="badge-status badge-status-open">🔴 Open</span>';

        const ackActionBtn = !isAck
            ? `<button class="btn btn-primary btn-sm" onclick="acknowledgeQCFault(${f.id})" title="Mark this fault as fixed/acknowledged">
                 ✅ Acknowledge
               </button>`
            : `<span class="text-xs text-muted">Fixed</span>`;

        const screenshotLink = f.image_url
            ? `<a href="javascript:void(0)" onclick="openQCScreenshotPreview('${escapeHTML(f.image_url)}')" class="qc-thumb-link">
                 🖼️ View Image
               </a>`
            : '<span class="text-muted text-xs">—</span>';

        return `
            <tr>
                <td><strong>#${f.id}</strong></td>
                <td>
                    <span class="font-bold text-main">📁 ${escapeHTML(f.project_code)}</span>
                </td>
                <td>
                    <span class="badge-sev ${sevClass}">${escapeHTML((f.severity || '').toUpperCase())}</span>
                </td>
                <td>${statusBadge}</td>
                <td>
                    <a href="javascript:void(0)" onclick="viewQCLabelerDetails('${escapeHTML(String(f.labeler_id))}')" class="font-semibold text-main hover-underline">
                        👤 ${escapeHTML(f.labeler_name)}
                    </a>
                    <div class="text-xs text-muted">ID: ${escapeHTML(String(f.labeler_id))}</div>
                </td>
                <td>
                    <span class="text-main">${escapeHTML(f.team_name || 'Unassigned')}</span>
                </td>
                <td>
                    <span class="qc-type-tag">${escapeHTML(f.fault_type || 'General')}</span>
                </td>
                <td style="max-width: 280px;" class="text-xs">${escapeHTML(f.description || '')}</td>
                <td>${screenshotLink}</td>
                <td class="text-xs text-muted">${escapeHTML(f.created_at || '')}</td>
                <td>${ackActionBtn}</td>
            </tr>
        `;
    }).join('');

    renderQCPagination(total, faults.length);
}

function renderQCPagination(total, currentCount) {
    const info = document.getElementById('qc-pagination-info');
    const prevBtn = document.getElementById('qc-prev-btn');
    const nextBtn = document.getElementById('qc-next-btn');

    if (!info) return;

    if (total === 0) {
        info.textContent = '0 records';
        if (prevBtn) prevBtn.disabled = true;
        if (nextBtn) nextBtn.disabled = true;
        return;
    }

    const start = qcFaultsOffset + 1;
    const end = Math.min(qcFaultsOffset + qcFaultsLimit, total);
    info.textContent = `Showing ${start} - ${end} of ${total} logs`;

    if (prevBtn) prevBtn.disabled = (qcFaultsOffset <= 0);
    if (nextBtn) nextBtn.disabled = (qcFaultsOffset + qcFaultsLimit >= total);
}

function qcPagePrev() {
    if (qcFaultsOffset > 0) {
        loadQCFaults(qcFaultsOffset - qcFaultsLimit);
    }
}

function qcPageNext() {
    loadQCFaults(qcFaultsOffset + qcFaultsLimit);
}

function resetQCFaultFilters() {
    const ids = ['qc-filter-project', 'qc-filter-team', 'qc-filter-severity', 'qc-filter-status', 'qc-filter-date-from', 'qc-filter-date-to', 'qc-filter-search'];
    ids.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = (id.includes('severity') || id.includes('status')) ? 'all' : '';
    });
    loadQCFaults(0);
}

function filterFaultsByProject(projectCode) {
    switchQCSubTab('faults');
    const el = document.getElementById('qc-filter-project');
    if (el) el.value = projectCode;
    loadQCFaults(0);
}

function filterFaultsByTeam(teamName) {
    switchQCSubTab('faults');
    const el = document.getElementById('qc-filter-team');
    if (el) el.value = teamName;
    loadQCFaults(0);
}

async function acknowledgeQCFault(faultId) {
    if (!confirm(`Mark Fault #${faultId} as Fixed / Acknowledged?`)) return;

    const res = await apiRequest(`/api/guild/${guildId}/qc/faults/${faultId}/ack`, 'POST', {});
    if (res && res.success) {
        showToast(res.message || `Fault #${faultId} acknowledged!`, 'success');
        loadQCFaults(qcFaultsOffset);
        // Refresh overview if cached
        loadQCOverview();
    } else {
        showToast(res?.error || 'Failed to acknowledge fault', 'error');
    }
}

function exportQCFaults(format) {
    const project = document.getElementById('qc-filter-project')?.value || '';
    const team = document.getElementById('qc-filter-team')?.value || '';
    const severity = document.getElementById('qc-filter-severity')?.value || '';
    const status = document.getElementById('qc-filter-status')?.value || '';
    const dateFrom = document.getElementById('qc-filter-date-from')?.value || '';
    const dateTo = document.getElementById('qc-filter-date-to')?.value || '';
    const search = document.getElementById('qc-filter-search')?.value || '';

    let url = `/api/guild/${guildId}/qc/export?format=${encodeURIComponent(format)}`;
    if (project) url += `&project_code=${encodeURIComponent(project)}`;
    if (team) url += `&team_name=${encodeURIComponent(team)}`;
    if (severity && severity !== 'all') url += `&severity=${encodeURIComponent(severity)}`;
    if (status && status !== 'all') url += `&status=${encodeURIComponent(status)}`;
    if (dateFrom) url += `&date_from=${encodeURIComponent(dateFrom)}`;
    if (dateTo) url += `&date_to=${encodeURIComponent(dateTo)}`;
    if (search) url += `&search=${encodeURIComponent(search)}`;

    showToast(`Generating ${format.toUpperCase()} export...`, 'info');
    window.open(url, '_blank');
}

// =============================================================================
// 5. Direct Web Fault Logging
// =============================================================================

function openQCLogFaultModal() {
    // Populate projects dropdown
    populateQCModalDropdowns();
    openQCModal('modal-qc-log-fault');
}

async function populateQCModalDropdowns() {
    const projSelect = document.getElementById('qc-lf-project');
    const teamSelect = document.getElementById('qc-lf-team');

    // Load projects
    const pData = await apiRequest(`/api/guild/${guildId}/qc/projects`, 'GET');
    if (pData && pData.success && projSelect) {
        projSelect.innerHTML = '<option value="">-- Select Active Project --</option>' +
            (pData.projects || []).filter(p => p.is_active).map(p =>
                `<option value="${escapeHTML(p.project_code)}">${escapeHTML(p.project_code)} — ${escapeHTML(p.project_name)}</option>`
            ).join('');
    }

    // Load teams
    const tData = await apiRequest(`/api/guild/${guildId}/qc/teams`, 'GET');
    if (tData && tData.success && teamSelect) {
        teamSelect.innerHTML = '<option value="">-- Auto-Detect from Labeler Roles --</option>' +
            (tData.teams || []).map(t =>
                `<option value="${t.id}">${escapeHTML(t.team_name)} (Role: ${escapeHTML(t.role_name || '')})</option>`
            ).join('');
    }
}

async function populateQCFaultFilterDropdowns() {
    const projSelect = document.getElementById('qc-filter-project');
    const teamSelect = document.getElementById('qc-filter-team');

    if (projSelect && projSelect.children.length <= 1) {
        const pData = await apiRequest(`/api/guild/${guildId}/qc/projects`, 'GET');
        if (pData && pData.success) {
            projSelect.innerHTML = '<option value="">All Projects</option>' +
                (pData.projects || []).map(p =>
                    `<option value="${escapeHTML(p.project_code)}">${escapeHTML(p.project_code)}</option>`
                ).join('');
        }
    }

    if (teamSelect && teamSelect.children.length <= 1) {
        const tData = await apiRequest(`/api/guild/${guildId}/qc/teams`, 'GET');
        if (tData && tData.success) {
            teamSelect.innerHTML = '<option value="">All Teams</option>' +
                (tData.teams || []).map(t =>
                    `<option value="${escapeHTML(t.team_name)}">${escapeHTML(t.team_name)}</option>`
                ).join('');
        }
    }
}

async function handleQCLogFaultSubmit(event) {
    event.preventDefault();

    const labelerIdRaw = String(document.getElementById('qc-lf-labeler-id').value).trim();
    if (!/^\d{17,20}$/.test(labelerIdRaw)) {
        showToast('Please select a member or enter a valid 18-19 digit Discord User ID', 'error');
        return;
    }

    const projectCode = document.getElementById('qc-lf-project').value.trim();
    if (!projectCode) {
        showToast('Please select a project code', 'error');
        return;
    }

    const severity = document.getElementById('qc-lf-severity').value;
    const description = document.getElementById('qc-lf-description').value.trim();
    if (!description) {
        showToast('Please provide a fault description / Task reference', 'error');
        return;
    }

    const faultType = document.getElementById('qc-lf-fault-type').value.trim() || null;
    const imageUrl = document.getElementById('qc-lf-image-url').value.trim() || null;
    const teamIdRaw = document.getElementById('qc-lf-team').value;
    const teamId = teamIdRaw ? parseInt(teamIdRaw) : null;
    const dispatchAlert = document.getElementById('qc-lf-dispatch-alert').checked;

    const payload = {
        labeler_id: labelerIdRaw,
        project_code: projectCode,
        severity: severity,
        description: description,
        fault_type: faultType,
        image_url: imageUrl,
        team_id: teamId,
        dispatch_discord_alert: dispatchAlert
    };

    const submitBtn = document.getElementById('qc-lf-submit-btn');
    if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = 'Logging Fault...';
    }

    const res = await apiRequest(`/api/guild/${guildId}/qc/faults`, 'POST', payload);

    if (submitBtn) {
        submitBtn.disabled = false;
        submitBtn.textContent = '🚀 Submit & Log Fault';
    }

    if (res && res.success) {
        showToast(`✅ Fault #${res.fault_id} logged successfully!${res.alert_posted ? ' (Alert posted to Discord)' : ''}`, 'success');
        closeQCModal('modal-qc-log-fault');
        document.getElementById('qc-log-fault-form').reset();
        // Refresh lists
        loadQCFaults(0);
        loadQCOverview();
    } else {
        showToast(res?.error || 'Failed to log fault', 'error');
    }
}

// =============================================================================
// 6. Projects Management
// =============================================================================

async function loadQCProjects() {
    const tbody = document.getElementById('qc-projects-table-tbody');
    if (!tbody) return;

    tbody.innerHTML = '<tr><td colspan="6" class="text-center py-4">⏳ Loading projects...</td></tr>';

    const data = await apiRequest(`/api/guild/${guildId}/qc/projects`, 'GET');
    if (!data || !data.success) {
        tbody.innerHTML = '<tr><td colspan="6" class="text-center text-danger py-4">Failed to load projects.</td></tr>';
        return;
    }

    const projects = data.projects || [];
    if (projects.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted py-6">No project codes added yet. Click "+ Add Project" to create one.</td></tr>';
        return;
    }

    tbody.innerHTML = projects.map(p => {
        const statusBadge = p.is_active
            ? '<span class="badge-status badge-status-active">🟢 Active</span>'
            : '<span class="badge-status badge-status-archived">⚪ Archived</span>';

        const toggleBtnLabel = p.is_active ? '📦 Archive' : '🟢 Activate';

        return `
            <tr>
                <td><strong class="text-main">📁 ${escapeHTML(p.project_code)}</strong></td>
                <td><span class="font-semibold">${escapeHTML(p.project_name)}</span></td>
                <td class="text-xs text-muted" style="max-width: 250px;">${escapeHTML(p.description || '—')}</td>
                <td>${statusBadge}</td>
                <td class="text-xs text-muted">${escapeHTML(p.created_at || '')}</td>
                <td>
                    <div class="d-flex gap-2">
                        <button class="btn btn-secondary btn-sm" onclick="toggleQCProjectStatus('${escapeHTML(p.project_code)}', ${p.is_active})">
                            ${toggleBtnLabel}
                        </button>
                        <button class="btn btn-danger btn-sm" onclick="deleteQCProject('${escapeHTML(p.project_code)}')">
                            🗑️ Delete
                        </button>
                    </div>
                </td>
            </tr>
        `;
    }).join('');
}

function openQCAddProjectModal() {
    document.getElementById('qc-proj-modal-title').textContent = '➕ Add New Project';
    document.getElementById('qc-project-form').reset();
    document.getElementById('qc-proj-code').readOnly = false;
    openQCModal('modal-qc-project');
}

async function handleQCProjectSubmit(event) {
    event.preventDefault();

    const code = document.getElementById('qc-proj-code').value.trim().toUpperCase();
    const name = document.getElementById('qc-proj-name').value.trim();
    const desc = document.getElementById('qc-proj-desc').value.trim() || null;
    const isActive = document.getElementById('qc-proj-active').checked;

    if (!code || !name) {
        showToast('Project Code and Project Name are required', 'error');
        return;
    }

    const payload = {
        project_code: code,
        project_name: name,
        description: desc,
        is_active: isActive
    };

    const res = await apiRequest(`/api/guild/${guildId}/qc/projects`, 'POST', payload);
    if (res && res.success) {
        showToast(`Project '${code}' created successfully!`, 'success');
        closeQCModal('modal-qc-project');
        loadQCProjects();
        loadQCOverview();
    } else {
        showToast(res?.error || 'Failed to create project', 'error');
    }
}

async function toggleQCProjectStatus(projectCode, currentStatus) {
    const newStatus = !currentStatus;
    const res = await apiRequest(`/api/guild/${guildId}/qc/projects/${encodeURIComponent(projectCode)}`, 'PUT', {
        is_active: newStatus
    });

    if (res && res.success) {
        showToast(`Project '${projectCode}' is now ${newStatus ? 'Active' : 'Archived'}`, 'success');
        loadQCProjects();
        loadQCOverview();
    } else {
        showToast(res?.error || 'Failed to update project status', 'error');
    }
}

async function deleteQCProject(projectCode) {
    if (!confirm(`Are you sure you want to permanently delete project '${projectCode}'?`)) return;

    const res = await apiRequest(`/api/guild/${guildId}/qc/projects/${encodeURIComponent(projectCode)}`, 'DELETE');
    if (res && res.success) {
        showToast(`Project '${projectCode}' deleted successfully`, 'success');
        loadQCProjects();
        loadQCOverview();
    } else {
        showToast(res?.error || 'Failed to delete project', 'error');
    }
}

// =============================================================================
// 7. Dynamic Teams & Role Mapping (10+ Teams Expandable)
// =============================================================================

async function loadQCTeams() {
    const tbody = document.getElementById('qc-teams-table-tbody');
    if (!tbody) return;

    tbody.innerHTML = '<tr><td colspan="6" class="text-center py-4">⏳ Loading teams...</td></tr>';

    const data = await apiRequest(`/api/guild/${guildId}/qc/teams`, 'GET');
    if (!data || !data.success) {
        tbody.innerHTML = '<tr><td colspan="6" class="text-center text-danger py-4">Failed to load teams.</td></tr>';
        return;
    }

    const teams = data.teams || [];
    if (teams.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted py-6">No teams configured yet. Click "+ Add Team" to configure role-mapped teams.</td></tr>';
        return;
    }

    tbody.innerHTML = teams.map(t => {
        const leaderHtml = t.leader_user_id
            ? `<div class="d-flex align-center gap-2">
                 ${t.leader_avatar ? `<img src="${t.leader_avatar}" class="qc-member-picker-avatar" alt="Avatar">` : ''}
                 <div>
                   <strong class="text-main">${escapeHTML(t.leader_display_name || t.leader_name || 'Leader')}</strong>
                   <div class="text-xs text-muted">ID: ${escapeHTML(String(t.leader_user_id))}</div>
                 </div>
               </div>`
            : '<span class="text-muted italic">Unassigned</span>';

        const channelName = t.team_channel_id
            ? `#${guildChannels.find(c => String(c.id) === String(t.team_channel_id))?.name || t.team_channel_id}`
            : '<span class="text-muted text-xs">Default Alert Channel</span>';

        return `
            <tr>
                <td><strong class="text-main">👥 ${escapeHTML(t.team_name)}</strong></td>
                <td>
                    <span class="server-tag" style="border-left: 3px solid ${escapeHTML(t.role_color || '#6366f1')};">
                        🛡️ ${escapeHTML(t.role_name || `Role #${t.team_role_id}`)}
                    </span>
                    <div class="text-xs text-muted">Role ID: ${escapeHTML(String(t.team_role_id))}</div>
                </td>
                <td>${leaderHtml}</td>
                <td><span class="text-xs">${channelName}</span></td>
                <td><span class="text-xs text-muted">${escapeHTML(t.created_at || '')}</span></td>
                <td>
                    <button class="btn btn-danger btn-sm" onclick="deleteQCTeam(${t.id}, '${escapeHTML(t.team_name)}')">
                        🗑️ Delete
                    </button>
                </td>
            </tr>
        `;
    }).join('');
}

function openQCAddTeamModal() {
    document.getElementById('qc-team-form').reset();
    document.getElementById('qc-team-id').value = '';
    populateQCTeamRoleOptions();
    openQCModal('modal-qc-team');
}

function populateQCTeamRoleOptions() {
    const roleSelect = document.getElementById('qc-team-role-select');
    const chSelect = document.getElementById('qc-team-channel-select');

    if (roleSelect) {
        roleSelect.innerHTML = '<option value="">-- Select Discord Team Role --</option>' +
            guildRoles.map(r => `<option value="${escapeHTML(String(r.id))}">${escapeHTML(r.name)}</option>`).join('');
    }

    if (chSelect) {
        chSelect.innerHTML = '<option value="">-- Default Alert Channel (from QC Settings) --</option>' +
            guildChannels.map(c => `<option value="${escapeHTML(String(c.id))}">#${escapeHTML(c.name)}</option>`).join('');
    }
}

async function handleQCTeamSubmit(event) {
    event.preventDefault();

    const teamName = document.getElementById('qc-team-name').value.trim();
    const roleIdRaw = String(document.getElementById('qc-team-role-select').value).trim();
    const leaderIdRaw = String(document.getElementById('qc-team-leader-id').value).trim();
    const channelIdRaw = String(document.getElementById('qc-team-channel-select').value).trim();

    if (!teamName) {
        showToast('Team Name is required', 'error');
        return;
    }
    if (!/^\d{17,20}$/.test(roleIdRaw)) {
        showToast('Please select a valid Discord Team Role', 'error');
        return;
    }
    if (leaderIdRaw && !/^\d{17,20}$/.test(leaderIdRaw)) {
        showToast('Team Leader ID must be a valid 18-19 digit Discord User ID', 'error');
        return;
    }

    const payload = {
        team_name: teamName,
        team_role_id: roleIdRaw,
        leader_user_id: leaderIdRaw || null,
        team_channel_id: channelIdRaw || null
    };

    const res = await apiRequest(`/api/guild/${guildId}/qc/teams`, 'POST', payload);
    if (res && res.success) {
        showToast(`Team '${teamName}' created successfully!`, 'success');
        closeQCModal('modal-qc-team');
        loadQCTeams();
        loadQCOverview();
    } else {
        showToast(res?.error || 'Failed to create team', 'error');
    }
}

async function deleteQCTeam(teamId, teamName) {
    if (!confirm(`Are you sure you want to delete team '${teamName}'?`)) return;

    const res = await apiRequest(`/api/guild/${guildId}/qc/teams/${teamId}`, 'DELETE');
    if (res && res.success) {
        showToast(`Team '${teamName}' deleted successfully`, 'success');
        loadQCTeams();
        loadQCOverview();
    } else {
        showToast(res?.error || 'Failed to delete team', 'error');
    }
}

// =============================================================================
// 8. Settings Management
// =============================================================================

async function loadQCSettings() {
    populateQCSettingsDropdowns();

    const data = await apiRequest(`/api/guild/${guildId}/qc/settings`, 'GET');
    if (!data || !data.success || !data.settings) return;

    const s = data.settings;
    const chSelect = document.getElementById('qc-settings-alert-channel');
    if (chSelect && s.alert_channel_id) {
        chSelect.value = String(s.alert_channel_id);
    }

    // Checker roles checkboxes
    const activeRoles = new Set((s.checker_role_ids || []).map(r => String(r)));
    document.querySelectorAll('.qc-checker-role-checkbox').forEach(cb => {
        cb.checked = activeRoles.has(String(cb.value));
    });
}

function populateQCSettingsDropdowns() {
    const chSelect = document.getElementById('qc-settings-alert-channel');
    if (chSelect && chSelect.children.length <= 1) {
        chSelect.innerHTML = '<option value="">-- No Default Channel Selected --</option>' +
            guildChannels.map(c => `<option value="${escapeHTML(String(c.id))}">#${escapeHTML(c.name)}</option>`).join('');
    }

    const rolesContainer = document.getElementById('qc-settings-checker-roles-container');
    if (rolesContainer && rolesContainer.children.length === 0) {
        rolesContainer.innerHTML = guildRoles.map(r => `
            <label class="d-flex align-center gap-2 text-sm" style="margin-bottom: 6px;">
                <input type="checkbox" class="qc-checker-role-checkbox" value="${escapeHTML(String(r.id))}">
                <span>${escapeHTML(r.name)} <span class="text-xs text-muted">(${escapeHTML(String(r.id))})</span></span>
            </label>
        `).join('');
    }
}

async function handleQCSettingsSubmit(event) {
    event.preventDefault();

    const chRaw = String(document.getElementById('qc-settings-alert-channel').value).trim();
    const alertChannelId = chRaw || null;

    const checkerRoles = [];
    document.querySelectorAll('.qc-checker-role-checkbox:checked').forEach(cb => {
        checkerRoles.push(String(cb.value).trim());
    });

    const payload = {
        alert_channel_id: alertChannelId,
        checker_role_ids: checkerRoles
    };

    const res = await apiRequest(`/api/guild/${guildId}/qc/settings`, 'POST', payload);
    if (res && res.success) {
        showToast('QC Settings saved successfully!', 'success');
    } else {
        showToast(res?.error || 'Failed to save QC settings', 'error');
    }
}

// =============================================================================
// 9. Searchable Member Picker Helper (64-Bit Safe)
// =============================================================================

function debounceQCMemberSearch(inputEl, resultsContainerId, targetIdInputId) {
    clearTimeout(qcMemberSearchTimer);
    const query = inputEl.value.trim();

    if (!query) {
        const container = document.getElementById(resultsContainerId);
        if (container) container.style.display = 'none';
        return;
    }

    // If admin pasted exact 18-19 digit ID, set it directly!
    if (/^\d{17,20}$/.test(query)) {
        document.getElementById(targetIdInputId).value = query;
    }

    qcMemberSearchTimer = setTimeout(async () => {
        const data = await apiRequest(`/api/guild/${guildId}/qc/members?search=${encodeURIComponent(query)}&limit=10`, 'GET');
        const container = document.getElementById(resultsContainerId);
        if (!container) return;

        if (!data || !data.success || !data.members || data.members.length === 0) {
            container.innerHTML = '<div class="p-2 text-muted text-xs">No matching members found</div>';
            container.style.display = 'block';
            return;
        }

        container.innerHTML = data.members.map(m => `
            <div class="qc-member-picker-item" onclick="selectQCMember('${escapeHTML(String(m.id))}', '${escapeHTML(m.display_name)}', '${inputEl.id}', '${targetIdInputId}', '${resultsContainerId}')">
                <img src="${m.avatar}" class="qc-member-picker-avatar" alt="Avatar">
                <div>
                    <strong>${escapeHTML(m.display_name)}</strong> <span class="text-xs text-muted">(@${escapeHTML(m.name)})</span>
                    <div class="text-xs text-muted">ID: ${escapeHTML(String(m.id))}</div>
                </div>
            </div>
        `).join('');
        container.style.display = 'block';
    }, 250);
}

function selectQCMember(memberId, memberDisplayName, inputElId, targetIdInputId, resultsContainerId) {
    // Strictly preserve 64-bit ID string
    const safeId = String(memberId).trim();
    document.getElementById(targetIdInputId).value = safeId;
    document.getElementById(inputElId).value = `${memberDisplayName} (${safeId})`;
    const container = document.getElementById(resultsContainerId);
    if (container) container.style.display = 'none';
}

// =============================================================================
// 10. Modals & Screenshot Preview
// =============================================================================

function openQCModal(modalId) {
    const el = document.getElementById(modalId);
    if (el) el.classList.add('active');
}

function closeQCModal(modalId) {
    const el = document.getElementById(modalId);
    if (el) el.classList.remove('active');
}

function openQCScreenshotPreview(imageUrl) {
    const img = document.getElementById('qc-screenshot-preview-img');
    const link = document.getElementById('qc-screenshot-preview-link');
    if (img) img.src = imageUrl;
    if (link) link.href = imageUrl;
    openQCModal('modal-qc-screenshot');
}

function escapeHTML(str) {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}
