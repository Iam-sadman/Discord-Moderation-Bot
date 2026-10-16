/**
 * tickets.js — Support Ticket System (Private Threads) Dashboard Controller
 * Full-featured interactive manager for Discord.py 2.x Private Thread Tickets.
 * Ensures strict 64-bit string ID serialization throughout all operations.
 */

// Module state
let currentPanels = [];
let activePanelId = null;
let currentTicketSettings = null;
let currentTicketCategories = [];
let currentTicketsList = [];
let serverChannels = (typeof window !== 'undefined' && window.guildChannels) ? window.guildChannels : [];
let serverRoles = (typeof window !== 'undefined' && window.guildRoles) ? window.guildRoles : [];
let serverMembers = [];

let selectedGlobalRoles = new Set();
let selectedGlobalMembers = new Set();
let selectedCatRoles = new Set();
let selectedCatMembers = new Set();

let currentViewingTicketId = null;
let currentEditingCategoryId = null;

// Safe HTML escape helper
function escapeHtml(text) {
    if (text === null || text === undefined) return '';
    const str = String(text);
    const map = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'};
    return str.replace(/[&<>"']/g, m => map[m]);
}

// =============================================================================
// 1. Initialization & Core Data Loading (Multiple Panels Support)
// =============================================================================

async function initTicketsTab() {
    console.log("[Tickets] Initializing Support Tickets tab for guild:", window.guildId);
    initSearchablePickers();
    try {
        await loadTicketPanels();
    } catch (e) {
        console.error("[Tickets] Error loading panels:", e);
    }
    try {
        await loadTicketsList();
    } catch (e) {
        console.error("[Tickets] Error loading tickets list:", e);
    }
}

async function loadTicketPanels() {
    try {
        const res = await apiRequest(`/api/guild/${window.guildId}/tickets/panels`, 'GET');
        if (!res || !res.success) {
            console.error("[Tickets] Failed to load panels:", res);
            const catContainer = document.getElementById('ticket-categories-list');
            if (catContainer) {
                catContainer.innerHTML = '<div class="text-muted text-center p-3">Failed to load panels. Click "Refresh" above to retry.</div>';
            }
            return;
        }

        currentPanels = res.panels || [];
        serverChannels = (res.channels && res.channels.length > 0) ? res.channels : (window.guildChannels || []);
        serverRoles = (res.roles && res.roles.length > 0) ? res.roles : (window.guildRoles || []);
        if (res.members && res.members.length > 0) {
            serverMembers = res.members;
        }

        // Ensure we have an active panel selected
        if (!activePanelId || !currentPanels.some(p => p.id === activePanelId)) {
            activePanelId = currentPanels.length > 0 ? currentPanels[0].id : null;
        }

        // Render Panel switcher tabs
        renderPanelSwitcher();
        populatePanelFilterDropdown();

        // Populate active panel config form
        if (activePanelId) {
            populateActivePanelForm();
            await loadTicketCategories(activePanelId);
        } else {
            const catContainer = document.getElementById('ticket-categories-list');
            if (catContainer) {
                catContainer.innerHTML = '<div class="text-muted text-center p-3">No panels configured. Click "➕ New Panel" to create your first ticket desk.</div>';
            }
        }

        // Update Statistics
        updateTicketStats(res.counts || {});

        // Initialize Searchable Pickers
        initSearchablePickers();
    } catch (err) {
        console.error("[Tickets] Error in loadTicketPanels:", err);
        const catContainer = document.getElementById('ticket-categories-list');
        if (catContainer) {
            catContainer.innerHTML = '<div class="text-muted text-center p-3">Failed to load panels. Click "Refresh" to retry.</div>';
        }
    }
}

function renderPanelSwitcher() {
    const nav = document.getElementById('ticket-panels-nav');
    if (!nav) return;
    nav.innerHTML = '';

    currentPanels.forEach(p => {
        const isActive = p.id === activePanelId;
        const pill = document.createElement('div');
        pill.className = `tickets-panel-pill ${isActive ? 'active' : ''}`;
        const isPublished = Boolean(p.message_id);
        const pubIcon = isPublished ? '🟢' : '⚪';
        pill.innerHTML = `
            <span>${pubIcon}</span>
            <span>${escapeHtml(p.name)}</span>
            <span class="badge-count" title="${p.category_count || 0} category button(s)">${p.category_count || 0}</span>
        `;
        pill.onclick = () => switchActivePanel(p.id);
        nav.appendChild(pill);
    });

    const delBtn = document.getElementById('btn-delete-active-panel');
    if (delBtn) {
        delBtn.style.display = currentPanels.length > 1 ? 'inline-flex' : 'none';
    }
}

async function switchActivePanel(panelId) {
    if (activePanelId === panelId) return;
    activePanelId = panelId;
    renderPanelSwitcher();
    populateActivePanelForm();
    await loadTicketCategories(activePanelId);
    updateDiscordPreview();
}

function populateActivePanelForm() {
    const panel = currentPanels.find(p => p.id === activePanelId);
    if (!panel) return;

    // Header updates
    const nameHeader = document.getElementById('active-panel-name-header');
    if (nameHeader) nameHeader.innerText = panel.name;
    const catTitle = document.getElementById('active-panel-cat-title');
    if (catTitle) catTitle.innerText = panel.name;

    // Status badge
    const badge = document.getElementById('active-panel-status-badge');
    if (badge) {
        if (panel.message_id) {
            badge.innerHTML = `<span class="tickets-panel-status-tag published" title="Message ID: ${panel.message_id}">🟢 Live on Discord (Msg #${panel.message_id})</span>`;
        } else {
            badge.innerHTML = `<span class="tickets-panel-status-tag">⚪ Not Published to Discord</span>`;
        }
    }

    // Form inputs
    const nameInput = document.getElementById('ticket-config-panel-name');
    if (nameInput) nameInput.value = panel.name;

    populateChannelDropdown('ticket-config-panel-channel', panel.channel_id);
    populateChannelDropdown('ticket-config-transcript-channel', panel.transcript_channel_id, true);

    const titleInput = document.getElementById('ticket-config-panel-title');
    if (titleInput) titleInput.value = panel.panel_title || '🛡️ Official Support & Issue Desk';

    const descInput = document.getElementById('ticket-config-panel-desc');
    if (descInput) descInput.value = panel.panel_description || 'Click a category button below to open a secure Private Thread.';

    const color = panel.panel_color || '#5865F2';
    const colorInput = document.getElementById('ticket-config-panel-color');
    const colorHexInput = document.getElementById('ticket-config-panel-color-hex');
    if (colorInput) colorInput.value = color;
    if (colorHexInput) colorHexInput.value = color;

    const maxOpenInput = document.getElementById('ticket-config-max-open');
    if (maxOpenInput) maxOpenInput.value = panel.max_open_per_user || 2;

    // Support Roles & Members
    selectedGlobalRoles = new Set((panel.support_role_ids || []).map(String));
    selectedGlobalMembers = new Set((panel.support_member_ids || []).map(String));
    renderGlobalRolesTags();
    renderGlobalMembersTags();

    updateDiscordPreview();
}

function handleActivePanelNameInput(val) {
    const clean = val.trim() || 'Ticket Panel';
    const nameHeader = document.getElementById('active-panel-name-header');
    if (nameHeader) nameHeader.innerText = clean;
    const catTitle = document.getElementById('active-panel-cat-title');
    if (catTitle) catTitle.innerText = clean;
    updateDiscordPreview();
}

function populatePanelFilterDropdown() {
    const select = document.getElementById('tickets-filter-panel');
    if (!select) return;
    const curVal = select.value;
    select.innerHTML = '<option value="">All Panels</option>';
    currentPanels.forEach(p => {
        const opt = document.createElement('option');
        opt.value = String(p.id);
        opt.textContent = `🎫 ${p.name}`;
        if (curVal && curVal === String(p.id)) opt.selected = true;
        select.appendChild(opt);
    });
}

// Modal: Create New Panel
function openCreatePanelModal() {
    document.getElementById('new-panel-name').value = '';
    populateChannelDropdown('new-panel-channel', '');
    populateChannelDropdown('new-panel-transcript-channel', '', true);
    document.getElementById('new-panel-title').value = '🛡️ Support Desk';
    document.getElementById('new-panel-desc').value = 'Click a category button below to open a secure Private Thread.';
    document.getElementById('new-panel-color').value = '#5865F2';
    document.getElementById('new-panel-color-hex').value = '#5865F2';
    document.getElementById('new-panel-max-open').value = 2;

    document.getElementById('modal-ticket-panel').style.display = 'flex';
}

function closeCreatePanelModal() {
    document.getElementById('modal-ticket-panel').style.display = 'none';
}

async function submitCreatePanelModal(event) {
    if (event) event.preventDefault();
    const name = document.getElementById('new-panel-name').value.trim();
    const chId = document.getElementById('new-panel-channel').value;
    const trId = document.getElementById('new-panel-transcript-channel').value;
    const title = document.getElementById('new-panel-title').value.trim();
    const desc = document.getElementById('new-panel-desc').value.trim();
    const color = document.getElementById('new-panel-color-hex').value.trim();
    const maxOpen = parseInt(document.getElementById('new-panel-max-open').value, 10) || 2;

    if (!name) {
        alert('Please specify a panel name.');
        return;
    }
    if (!chId) {
        alert('Please choose a designated Discord panel channel.');
        return;
    }

    const payload = {
        name: name,
        channel_id: chId,
        transcript_channel_id: trId || null,
        panel_title: title || '🛡️ Support Desk',
        panel_description: desc || 'Click a category button below to open a secure Private Thread.',
        panel_color: color || '#5865F2',
        max_open_per_user: maxOpen
    };

    const res = await apiRequest(`/api/guild/${window.guildId}/tickets/panels`, 'POST', payload);
    if (res && res.success) {
        closeCreatePanelModal();
        activePanelId = res.panel.id;
        await loadTicketPanels();
        alert(`✅ Ticket panel "${name}" created successfully! You can now add category buttons and publish it to Discord.`);
    } else {
        alert(res?.error || 'Failed to create ticket panel.');
    }
}

async function deleteActivePanelPrompt() {
    if (currentPanels.length <= 1) {
        alert('⚠️ Cannot delete the only ticket panel in the server.');
        return;
    }
    const panel = currentPanels.find(p => p.id === activePanelId);
    if (!panel) return;

    if (!confirm(`🗑️ Are you sure you want to delete panel "${panel.name}"?\n\n- All associated category buttons will be removed.\n- If published on Discord, the message embed will be deleted.`)) {
        return;
    }

    const res = await apiRequest(`/api/guild/${window.guildId}/tickets/panels/${activePanelId}`, 'DELETE');
    if (res && res.success) {
        activePanelId = null;
        await loadTicketPanels();
        alert('✅ Ticket panel deleted successfully.');
    } else {
        alert(res?.error || 'Failed to delete panel.');
    }
}

function updateTicketStats(counts) {
    document.getElementById('stat-tickets-total').innerText = counts.total || 0;
    document.getElementById('stat-tickets-open').innerText = counts.open || 0;
    document.getElementById('stat-tickets-claimed').innerText = counts.claimed || 0;
    document.getElementById('stat-tickets-closed').innerText = counts.closed || 0;
}

function populateChannelDropdown(elemId, selectedId, allowNone = false) {
    const select = document.getElementById(elemId);
    if (!select) return;

    select.innerHTML = '';
    if (allowNone) {
        const optNone = document.createElement('option');
        optNone.value = '';
        optNone.textContent = 'None / Disabled (Do not log to Discord channel)';
        select.appendChild(optNone);
    } else {
        const optPrompt = document.createElement('option');
        optPrompt.value = '';
        optPrompt.textContent = '-- Select Discord Channel --';
        select.appendChild(optPrompt);
    }

    serverChannels.forEach(ch => {
        const opt = document.createElement('option');
        opt.value = String(ch.id);
        opt.textContent = ch.label || `#${ch.name}`;
        if (selectedId && String(ch.id) === String(selectedId)) {
            opt.selected = true;
        }
        select.appendChild(opt);
    });
}


// =============================================================================
// 2. Multi-Select Role & Member Tag Pickers
// =============================================================================

function renderGlobalRolesTags() {
    const container = document.getElementById('ticket-global-roles-tags');
    if (!container) return;
    container.innerHTML = '';

    if (selectedGlobalRoles.size === 0) {
        container.innerHTML = '<span class="text-xs text-muted">Server Administrators only (Click to add support roles)</span>';
        return;
    }

    selectedGlobalRoles.forEach(roleId => {
        const role = serverRoles.find(r => String(r.id) === String(roleId));
        const roleName = role ? role.name : `Role #${roleId}`;
        const badge = document.createElement('span');
        badge.className = 'tag-badge';
        badge.innerHTML = `@${escapeHtml(roleName)} <span class="tag-remove" onclick="removeGlobalRole('${roleId}', event)">&times;</span>`;
        container.appendChild(badge);
    });
}

function removeGlobalRole(roleId, evt) {
    if (evt) evt.stopPropagation();
    selectedGlobalRoles.delete(String(roleId));
    renderGlobalRolesTags();
    const drop = document.getElementById('ticket-global-roles-dropdown');
    if (drop && drop.style.display === 'block') {
        const input = document.getElementById('ticket-global-roles-search-input');
        if (input) input.dispatchEvent(new Event('input'));
    }
}

function removeGlobalMember(userId, evt) {
    if (evt) evt.stopPropagation();
    selectedGlobalMembers.delete(String(userId));
    renderGlobalMembersTags();
    const drop = document.getElementById('ticket-global-members-dropdown');
    if (drop && drop.style.display === 'block') {
        const input = document.getElementById('ticket-global-members-search-input');
        if (input) input.dispatchEvent(new Event('input'));
    }
}

function setupSearchableTagPicker(inputId, dropdownId, getItemsFn, getSelectedSetFn, renderTagsCallback, isMember = false) {
    const input = document.getElementById(inputId);
    const dropdown = document.getElementById(dropdownId);
    if (!input || !dropdown) return;

    let memberSearchTimer = null;

    async function renderList(query = '') {
        const rawItems = getItemsFn() || [];
        const items = (!isMember && rawItems.length === 0 && window.guildRoles) ? window.guildRoles : rawItems;
        dropdown.innerHTML = '';
        const q = query.trim().toLowerCase();

        const selectedSet = typeof getSelectedSetFn === 'function' ? getSelectedSetFn() : getSelectedSetFn;

        const filtered = items.filter(item => {
            if (!q) return true;
            if (isMember) {
                const name = (item.name || '').toLowerCase();
                const dName = (item.display_name || '').toLowerCase();
                const id = String(item.id);
                return dName.includes(q) || name.includes(q) || id.includes(q);
            } else {
                return (item.name || '').toLowerCase().includes(q) || String(item.id).includes(q);
            }
        });

        // Direct snowflake ID support for members
        if (isMember && /^\d{17,20}$/.test(query.trim()) && !filtered.some(i => String(i.id) === query.trim())) {
            filtered.unshift({
                id: query.trim(),
                name: `User #${query.trim()}`,
                display_name: `Discord User (${query.trim()})`
            });
        }

        if (filtered.length === 0) {
            dropdown.innerHTML = `<div class="p-3 text-xs text-muted text-center">${isMember && q.length > 0 ? '🔍 Searching server members...' : 'No matching results found'}</div>`;
        } else {
            // Render up to 50 items
            filtered.slice(0, 50).forEach(item => {
                const isSelected = selectedSet && selectedSet.has(String(item.id));
                const row = document.createElement('div');
                row.className = 'tag-dropdown-item' + (isSelected ? ' selected' : '');
                
                let label = '';
                if (isMember) {
                    const dName = item.display_name || item.name;
                    const uName = item.name && item.name !== dName ? ` (@${escapeHtml(item.name)})` : '';
                    label = `<span>👤 <strong>${escapeHtml(dName)}</strong>${uName} <small class="text-muted" style="font-size: 11px; margin-left: 6px;">ID: ${item.id}</small></span>`;
                } else {
                    label = `<span>@<strong>${escapeHtml(item.name)}</strong></span>`;
                }

                row.innerHTML = `${label}<span class="tag-check">${isSelected ? '✅' : '➕'}</span>`;
                
                row.onclick = (e) => {
                    e.stopPropagation();
                    const curSet = typeof getSelectedSetFn === 'function' ? getSelectedSetFn() : getSelectedSetFn;
                    if (curSet.has(String(item.id))) {
                        curSet.delete(String(item.id));
                    } else {
                        curSet.add(String(item.id));
                    }
                    renderTagsCallback();
                    const nowSelected = curSet.has(String(item.id));
                    row.className = 'tag-dropdown-item' + (nowSelected ? ' selected' : '');
                    const check = row.querySelector('.tag-check');
                    if (check) check.innerText = nowSelected ? '✅' : '➕';
                    input.focus();
                };
                dropdown.appendChild(row);
            });
        }

        // Live backend member search if query is typed
        if (isMember && q.length >= 1) {
            clearTimeout(memberSearchTimer);
            memberSearchTimer = setTimeout(async () => {
                try {
                    const data = await apiRequest(`/api/guild/${window.guildId}/roles/members?search=${encodeURIComponent(q)}&limit=30`, 'GET');
                    if (data && data.members && data.members.length > 0) {
                        let mergedAny = false;
                        data.members.forEach(m => {
                            if (!serverMembers.some(ex => String(ex.id) === String(m.id))) {
                                serverMembers.push({
                                    id: String(m.id),
                                    name: m.name,
                                    display_name: m.display_name || m.name
                                });
                                mergedAny = true;
                            }
                        });
                        // Re-render only if current input still matches query
                        if (mergedAny && input.value.trim().toLowerCase() === q) {
                            renderList(input.value);
                        }
                    }
                } catch (err) {
                    console.warn("[Tickets] Member live search notice:", err);
                }
            }, 250);
        }
    }

    input.onfocus = () => {
        renderList(input.value);
        dropdown.style.display = 'block';
    };

    input.oninput = (e) => {
        renderList(e.target.value);
        dropdown.style.display = 'block';
    };

    input.onclick = (e) => {
        e.stopPropagation();
        renderList(input.value);
        dropdown.style.display = 'block';
    };
}

function initSearchablePickers() {
    setupSearchableTagPicker(
        'ticket-global-roles-search-input',
        'ticket-global-roles-dropdown',
        () => (serverRoles && serverRoles.length > 0 ? serverRoles : (window.guildRoles || [])),
        () => selectedGlobalRoles,
        renderGlobalRolesTags,
        false
    );
    setupSearchableTagPicker(
        'ticket-global-members-search-input',
        'ticket-global-members-dropdown',
        () => serverMembers,
        () => selectedGlobalMembers,
        renderGlobalMembersTags,
        true
    );
    setupSearchableTagPicker(
        'cat-roles-search-input',
        'cat-roles-dropdown',
        () => (serverRoles && serverRoles.length > 0 ? serverRoles : (window.guildRoles || [])),
        () => selectedCatRoles,
        renderCatRolesTags,
        false
    );
    setupSearchableTagPicker(
        'cat-members-search-input',
        'cat-members-dropdown',
        () => serverMembers,
        () => selectedCatMembers,
        renderCatMembersTags,
        true
    );
}

// Close tag dropdowns on clicking outside
document.addEventListener('click', (e) => {
    if (!e.target.closest('#ticket-global-roles-picker')) {
        const drop = document.getElementById('ticket-global-roles-dropdown');
        if (drop) drop.style.display = 'none';
    }
    if (!e.target.closest('#ticket-global-members-picker')) {
        const drop = document.getElementById('ticket-global-members-dropdown');
        if (drop) drop.style.display = 'none';
    }
    if (!e.target.closest('#cat-roles-picker')) {
        const drop = document.getElementById('cat-roles-dropdown');
        if (drop) drop.style.display = 'none';
    }
    if (!e.target.closest('#cat-members-picker')) {
        const drop = document.getElementById('cat-members-dropdown');
        if (drop) drop.style.display = 'none';
    }
});

// Color Picker synchronization
function handleTicketColorChange(val) {
    let raw = String(val || '').trim();
    let safeHex = raw;
    if (!safeHex.startsWith('#') && /^[0-9a-fA-F]{3,8}$/.test(safeHex)) {
        safeHex = '#' + safeHex;
    }

    const hexInput = document.getElementById('ticket-config-panel-color-hex');
    const colorInput = document.getElementById('ticket-config-panel-color');

    if (hexInput && hexInput.value !== raw) {
        hexInput.value = raw;
    }
    if (colorInput && /^#[0-9a-fA-F]{6}$/i.test(safeHex)) {
        colorInput.value = safeHex;
    }
    updateDiscordPreview();
}

// =============================================================================
// 3. Save Settings & Publish Panel (Active Panel Support)
// =============================================================================

async function saveTicketSettings() {
    const name = document.getElementById('ticket-config-panel-name')?.value.trim() || 'Support Desk';
    const panelCh = document.getElementById('ticket-config-panel-channel').value;
    const transCh = document.getElementById('ticket-config-transcript-channel').value;
    const title = document.getElementById('ticket-config-panel-title').value.trim();
    const desc = document.getElementById('ticket-config-panel-desc').value.trim();
    const color = document.getElementById('ticket-config-panel-color-hex').value.trim();
    const maxOpen = parseInt(document.getElementById('ticket-config-max-open').value, 10) || 2;

    const payload = {
        name: name,
        channel_id: panelCh || null,
        transcript_channel_id: transCh || null,
        support_role_ids: Array.from(selectedGlobalRoles),
        support_member_ids: Array.from(selectedGlobalMembers),
        panel_title: title,
        panel_description: desc,
        panel_color: color,
        max_open_per_user: maxOpen
    };

    let res;
    if (activePanelId) {
        res = await apiRequest(`/api/guild/${window.guildId}/tickets/panels/${activePanelId}`, 'PUT', payload);
        if (res && res.success) {
            const idx = currentPanels.findIndex(p => p.id === activePanelId);
            if (idx !== -1) {
                currentPanels[idx] = res.panel;
            }
            renderPanelSwitcher();
            populateActivePanelForm();
            alert('✅ Ticket panel configuration saved successfully!');
            return;
        }
    } else {
        res = await apiRequest(`/api/guild/${window.guildId}/tickets/settings`, 'POST', payload);
        if (res && res.success) {
            currentTicketSettings = res.settings;
            updateDiscordPreview();
            alert('✅ Ticket configuration saved successfully!');
            return;
        }
    }
    alert(res?.error || 'Failed to save ticket settings.');
}

async function publishTicketPanel() {
    const panelCh = document.getElementById('ticket-config-panel-channel').value;
    if (!panelCh) {
        alert('⚠️ Please select and save a designated Ticket Panel Channel before publishing.');
        return;
    }

    const panel = currentPanels.find(p => p.id === activePanelId);
    const panelName = panel ? panel.name : 'Ticket Panel';

    if (!confirm(`🚀 Publish / Update live Ticket Panel "${panelName}" embed & category buttons to Discord now?`)) {
        return;
    }

    // Save first to ensure Discord receives latest changes
    await saveTicketSettings();

    let res;
    if (activePanelId) {
        res = await apiRequest(`/api/guild/${window.guildId}/tickets/panels/${activePanelId}/publish`, 'POST', {});
        if (res && res.success) {
            if (panel) {
                panel.message_id = res.panel_message_id;
            }
            renderPanelSwitcher();
            populateActivePanelForm();
            alert(`✅ ${res.message || 'Ticket panel successfully updated on Discord!'}`);
            return;
        }
    } else {
        res = await apiRequest(`/api/guild/${window.guildId}/tickets/publish`, 'POST', {});
        if (res && res.success) {
            alert(`✅ ${res.message || 'Ticket panel successfully updated on Discord!'}`);
            return;
        }
    }
    alert(`❌ Failed to publish panel: ${res?.error || 'Unknown error'}`);
}

// =============================================================================
// 4. Live Discord Panel Preview
// =============================================================================

function updateDiscordPreview() {
    const titleInput = document.getElementById('ticket-config-panel-title');
    const descInput = document.getElementById('ticket-config-panel-desc');
    const colorHexInput = document.getElementById('ticket-config-panel-color-hex');
    const nameInput = document.getElementById('ticket-config-panel-name');

    const title = titleInput?.value || '🛡️ Official Support & Issue Desk';
    const desc = descInput?.value || 'Click a category button below to open a secure Private Thread.';
    const panelName = nameInput?.value?.trim() || 'Main Support Desk';

    let color = colorHexInput?.value?.trim() || '#5865F2';
    if (!color.startsWith('#') && /^[0-9a-fA-F]{3,8}$/.test(color)) {
        color = '#' + color;
    }
    if (!/^#[0-9a-fA-F]{3,8}$/i.test(color)) {
        color = '#5865F2';
    }

    const pTitle = document.getElementById('discord-preview-title');
    const pDesc = document.getElementById('discord-preview-desc');
    const pEmbed = document.getElementById('discord-preview-embed');
    const pButtons = document.getElementById('discord-preview-buttons');
    const pFooterText = document.getElementById('discord-preview-footer-text');

    if (pTitle) pTitle.innerText = title;
    if (pDesc) pDesc.innerText = desc;
    if (pEmbed) {
        pEmbed.style.borderLeft = `4px solid ${color}`;
        pEmbed.style.borderLeftColor = color;
    }
    if (pFooterText) {
        pFooterText.innerText = `${panelName} • Private Threads • Only assigned staff & ticket creator have access`;
    }

    if (pButtons) {
        pButtons.innerHTML = '';
        const cats = Array.isArray(currentTicketCategories) ? currentTicketCategories : [];
        if (cats.length === 0) {
            pButtons.innerHTML = '<span class="text-xs text-muted" style="grid-column: 1 / -1; padding: 6px 0;">No category buttons configured for this panel yet. Add buttons below.</span>';
            return;
        }

        cats.forEach(cat => {
            if (!cat) return;
            const btn = document.createElement('div');
            const style = (cat.button_style || 'primary').toLowerCase();
            btn.className = `discord-btn-mock discord-btn-${style}`;
            btn.style.cursor = 'pointer';
            btn.title = `Simulate click: ${cat.name}`;
            btn.innerHTML = `<span>${cat.emoji || '🎫'}</span> <span>${escapeHtml(cat.name || 'Support')}</span>`;
            btn.onclick = () => {
                if (typeof showToast === 'function') {
                    showToast(`Simulated click: In Discord, "${cat.name}" opens a modal to launch a Private Thread.`, 'info');
                }
            };
            pButtons.appendChild(btn);
        });
    }
}

// =============================================================================
// 5. Category Buttons Builder
// =============================================================================

async function loadTicketCategories(panelId = null) {
    const container = document.getElementById('ticket-categories-list');
    try {
        const pId = panelId || activePanelId;
        const url = pId ? `/api/guild/${window.guildId}/tickets/categories?panel_id=${pId}` : `/api/guild/${window.guildId}/tickets/categories`;
        const res = await apiRequest(url, 'GET');
        if (!res || !res.success) {
            console.error("[Tickets] Failed to load categories:", res);
            if (container) {
                container.innerHTML = '<div class="text-muted text-center p-3">No categories found or failed to load. Click "➕ Add Category Button" to create one.</div>';
            }
            return;
        }
        currentTicketCategories = res.categories || [];
        renderCategoriesList();
        updateDiscordPreview();
        populateCategoryFilterDropdown();

        // Update active panel category count badge in switcher
        if (pId) {
            const panel = currentPanels.find(p => p.id === pId);
            if (panel) {
                panel.category_count = currentTicketCategories.length;
                renderPanelSwitcher();
            }
        }
    } catch (err) {
        console.error("[Tickets] Error in loadTicketCategories:", err);
        if (container) {
            container.innerHTML = '<div class="text-muted text-center p-3">No categories available. Click "➕ Add Category Button" to create one.</div>';
        }
    }
}

function renderCategoriesList() {
    const container = document.getElementById('ticket-categories-list');
    if (!container) return;

    container.innerHTML = '';
    if (currentTicketCategories.length === 0) {
        container.innerHTML = '<div class="text-muted text-center p-3">No categories available. Click "➕ Add Category Button" to create one.</div>';
        return;
    }

    currentTicketCategories.forEach((cat, idx) => {
        const card = document.createElement('div');
        card.className = 'category-row-card';

        // Role badges
        const roleBadges = (cat.assigned_role_ids || []).map(rid => {
            const role = serverRoles.find(r => String(r.id) === String(rid));
            const rName = role ? role.name : `Role #${rid}`;
            return `<span class="tag-badge">@${escapeHtml(rName)}</span>`;
        }).join('');

        // Member badges
        const memberBadges = (cat.assigned_member_ids || []).map(mid => {
            const m = serverMembers.find(u => String(u.id) === String(mid));
            const mName = m ? (m.display_name || m.name) : `User #${mid}`;
            return `<span class="tag-badge tag-badge-member">👤 ${escapeHtml(mName)}</span>`;
        }).join('');

        const styleBadge = `<span class="discord-btn-mock discord-btn-${(cat.button_style || 'primary').toLowerCase()}" style="padding: 2px 8px; font-size: 11px;">${cat.button_style || 'primary'}</span>`;

        card.innerHTML = `
            <div class="category-info-col">
                <div class="category-emoji-box">${cat.emoji || '🎫'}</div>
                <div class="category-meta">
                    <div class="category-meta-title">
                        <span>${escapeHtml(cat.name)}</span>
                        ${styleBadge}
                    </div>
                    <div class="category-meta-desc">${escapeHtml(cat.description || 'No description provided.')}</div>
                    <div class="category-routings">
                        ${roleBadges || '<span class="text-xs text-muted">Inherits global support roles</span>'}
                        ${memberBadges}
                    </div>
                </div>
            </div>
            <div class="category-actions-col">
                <button class="btn btn-secondary btn-sm" onclick="moveCategory(${cat.id}, -1)" ${idx === 0 ? 'disabled' : ''} title="Move Up">↑</button>
                <button class="btn btn-secondary btn-sm" onclick="moveCategory(${cat.id}, 1)" ${idx === currentTicketCategories.length - 1 ? 'disabled' : ''} title="Move Down">↓</button>
                <button class="btn btn-secondary btn-sm" onclick="openEditCategoryModal(${cat.id})">✏️ Edit</button>
                <button class="btn btn-danger btn-sm" onclick="deleteCategory(${cat.id})">🗑️</button>
            </div>
        `;
        container.appendChild(card);
    });
}

function openCreateCategoryModal() {
    currentEditingCategoryId = null;
    document.getElementById('cat-modal-title').innerText = '➕ Create Ticket Category Button';
    document.getElementById('cat-name').value = '';
    document.getElementById('cat-emoji').value = '🎫';
    document.getElementById('cat-style').value = 'primary';
    document.getElementById('cat-desc').value = '';

    const rSearch = document.getElementById('cat-roles-search-input');
    if (rSearch) rSearch.value = '';
    const mSearch = document.getElementById('cat-members-search-input');
    if (mSearch) mSearch.value = '';
    const rDrop = document.getElementById('cat-roles-dropdown');
    if (rDrop) rDrop.style.display = 'none';
    const mDrop = document.getElementById('cat-members-dropdown');
    if (mDrop) mDrop.style.display = 'none';

    selectedCatRoles = new Set();
    selectedCatMembers = new Set();
    renderCatRolesTags();
    renderCatMembersTags();

    updateCatModalPreview();
    document.getElementById('modal-ticket-category').style.display = 'flex';
}

function openEditCategoryModal(catId) {
    const cat = currentTicketCategories.find(c => c.id === catId);
    if (!cat) return;

    currentEditingCategoryId = catId;
    document.getElementById('cat-modal-title').innerText = '✏️ Edit Category Button';
    document.getElementById('cat-name').value = cat.name;
    document.getElementById('cat-emoji').value = cat.emoji || '🎫';
    document.getElementById('cat-style').value = cat.button_style || 'primary';
    document.getElementById('cat-desc').value = cat.description || '';

    const rSearch = document.getElementById('cat-roles-search-input');
    if (rSearch) rSearch.value = '';
    const mSearch = document.getElementById('cat-members-search-input');
    if (mSearch) mSearch.value = '';
    const rDrop = document.getElementById('cat-roles-dropdown');
    if (rDrop) rDrop.style.display = 'none';
    const mDrop = document.getElementById('cat-members-dropdown');
    if (mDrop) mDrop.style.display = 'none';

    selectedCatRoles = new Set((cat.assigned_role_ids || []).map(String));
    selectedCatMembers = new Set((cat.assigned_member_ids || []).map(String));
    renderCatRolesTags();
    renderCatMembersTags();

    updateCatModalPreview();
    document.getElementById('modal-ticket-category').style.display = 'flex';
}

function updateCatModalPreview() {
    const label = document.getElementById('cat-name')?.value?.trim() || 'Category Button';
    const emoji = document.getElementById('cat-emoji')?.value?.trim() || '🎫';
    const style = (document.getElementById('cat-style')?.value || 'primary').toLowerCase();

    const btn = document.getElementById('modal-cat-preview-btn');
    const pEmoji = document.getElementById('modal-cat-preview-emoji');
    const pLabel = document.getElementById('modal-cat-preview-label');

    if (btn) {
        btn.className = `discord-btn-mock discord-btn-${style}`;
    }
    if (pEmoji) pEmoji.innerText = emoji;
    if (pLabel) pLabel.innerText = label;
}

function closeCategoryModal() {
    document.getElementById('modal-ticket-category').style.display = 'none';
}

function renderCatRolesTags() {
    const container = document.getElementById('cat-roles-tags');
    if (!container) return;
    container.innerHTML = '';

    if (selectedCatRoles.size === 0) {
        container.innerHTML = '<span class="text-xs text-muted">No roles assigned (Inherits global support roles)</span>';
        return;
    }

    selectedCatRoles.forEach(roleId => {
        const role = serverRoles.find(r => String(r.id) === String(roleId));
        const roleName = role ? role.name : `Role #${roleId}`;
        const badge = document.createElement('span');
        badge.className = 'tag-badge';
        badge.innerHTML = `@${escapeHtml(roleName)} <span class="tag-remove" onclick="removeCatRole('${roleId}', event)">&times;</span>`;
        container.appendChild(badge);
    });
}

function removeCatRole(roleId, evt) {
    if (evt) evt.stopPropagation();
    selectedCatRoles.delete(String(roleId));
    renderCatRolesTags();
    const drop = document.getElementById('cat-roles-dropdown');
    if (drop && drop.style.display === 'block') {
        const input = document.getElementById('cat-roles-search-input');
        if (input) input.dispatchEvent(new Event('input'));
    }
}

function toggleCatRolesDropdown() {
    const input = document.getElementById('cat-roles-search-input');
    if (input) input.focus();
}

function renderCatMembersTags() {
    const container = document.getElementById('cat-members-tags');
    if (!container) return;
    container.innerHTML = '';

    if (selectedCatMembers.size === 0) {
        container.innerHTML = '<span class="text-xs text-muted">No specific members assigned</span>';
        return;
    }

    selectedCatMembers.forEach(userId => {
        const member = serverMembers.find(m => String(m.id) === String(userId));
        const memberName = member ? (member.display_name || member.name) : `User #${userId}`;
        const badge = document.createElement('span');
        badge.className = 'tag-badge tag-badge-member';
        badge.innerHTML = `👤 ${escapeHtml(memberName)} <span class="tag-remove" onclick="removeCatMember('${userId}', event)">&times;</span>`;
        container.appendChild(badge);
    });
}

function removeCatMember(userId, evt) {
    if (evt) evt.stopPropagation();
    selectedCatMembers.delete(String(userId));
    renderCatMembersTags();
    const drop = document.getElementById('cat-members-dropdown');
    if (drop && drop.style.display === 'block') {
        const input = document.getElementById('cat-members-search-input');
        if (input) input.dispatchEvent(new Event('input'));
    }
}

function toggleCatMembersDropdown() {
    const input = document.getElementById('cat-members-search-input');
    if (input) input.focus();
}

async function saveCategorySubmit(event) {
    if (event) event.preventDefault();
    const name = document.getElementById('cat-name').value.trim();
    const emoji = document.getElementById('cat-emoji').value.trim() || '🎫';
    const style = document.getElementById('cat-style').value;
    const desc = document.getElementById('cat-desc').value.trim();

    if (!name) {
        alert('Please provide a category button name.');
        return;
    }

    const payload = {
        name: name,
        emoji: emoji,
        button_style: style,
        description: desc,
        assigned_role_ids: Array.from(selectedCatRoles),
        assigned_member_ids: Array.from(selectedCatMembers),
        panel_id: activePanelId
    };

    let res;
    if (currentEditingCategoryId) {
        res = await apiRequest(`/api/guild/${window.guildId}/tickets/categories/${currentEditingCategoryId}`, 'PUT', payload);
    } else {
        res = await apiRequest(`/api/guild/${window.guildId}/tickets/categories`, 'POST', payload);
    }

    if (res && res.success) {
        closeCategoryModal();
        await loadTicketCategories(activePanelId);
    } else {
        alert(res?.error || 'Failed to save category button.');
    }
}

async function deleteCategory(catId) {
    if (!confirm('Are you sure you want to delete this ticket category button?')) return;
    const res = await apiRequest(`/api/guild/${window.guildId}/tickets/categories/${catId}`, 'DELETE');
    if (res && res.success) {
        await loadTicketCategories(activePanelId);
    } else {
        alert(res?.error || 'Failed to delete category.');
    }
}

async function moveCategory(catId, delta) {
    const idx = currentTicketCategories.findIndex(c => c.id === catId);
    if (idx === -1) return;
    const targetIdx = idx + delta;
    if (targetIdx < 0 || targetIdx >= currentTicketCategories.length) return;

    const copy = [...currentTicketCategories];
    const temp = copy[idx];
    copy[idx] = copy[targetIdx];
    copy[targetIdx] = temp;

    const orderedIds = copy.map(c => c.id);
    const res = await apiRequest(`/api/guild/${window.guildId}/tickets/categories/reorder`, 'POST', { category_ids: orderedIds });
    if (res && res.success) {
        await loadTicketCategories(activePanelId);
    }
}

function populateCategoryFilterDropdown() {
    const select = document.getElementById('tickets-filter-category');
    if (!select) return;
    const curVal = select.value;
    select.innerHTML = '<option value="">All Categories</option>';
    currentTicketCategories.forEach(c => {
        const opt = document.createElement('option');
        opt.value = String(c.id);
        opt.textContent = `${c.emoji || '🎫'} ${c.name}`;
        if (curVal && curVal === String(c.id)) opt.selected = true;
        select.appendChild(opt);
    });
}

// =============================================================================
// 6. Active & Closed Tickets Table & Actions
// =============================================================================

async function loadTicketsList() {
    const tbody = document.getElementById('tickets-table-body');
    try {
        const status = document.getElementById('tickets-filter-status')?.value || 'all';
        const categoryId = document.getElementById('tickets-filter-category')?.value || '';
        const panelId = document.getElementById('tickets-filter-panel')?.value || '';
        const search = document.getElementById('tickets-search-input')?.value || '';

        const params = new URLSearchParams();
        if (status) params.append('status', status);
        if (categoryId) params.append('category_id', categoryId);
        if (panelId) params.append('panel_id', panelId);
        if (search) params.append('search', search);

        const res = await apiRequest(`/api/guild/${window.guildId}/tickets?${params.toString()}`, 'GET');
        if (!res || !res.success) {
            console.error("[Tickets] Failed to load tickets:", res);
            if (tbody) {
                tbody.innerHTML = '<tr><td colspan="8" class="text-center text-muted p-4">No tickets recorded or unable to load tickets.</td></tr>';
            }
            return;
        }

        currentTicketsList = res.tickets || [];
        renderTicketsTable();
        updateTicketStats(res.counts || {});
    } catch (err) {
        console.error("[Tickets] Error in loadTicketsList:", err);
        if (tbody) {
            tbody.innerHTML = '<tr><td colspan="8" class="text-center text-muted p-4">No tickets recorded.</td></tr>';
        }
    }
}

function renderTicketsTable() {
    const tbody = document.getElementById('tickets-table-body');
    if (!tbody) return;

    tbody.innerHTML = '';
    if (currentTicketsList.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" class="text-center text-muted p-4">No tickets matching the filter criteria.</td></tr>';
        return;
    }

    currentTicketsList.forEach(t => {
        const tr = document.createElement('tr');

        // Status badge
        let statusBadge = '<span class="ticket-status-badge status-badge-open">🟢 Open</span>';
        if (t.status === 'claimed') {
            statusBadge = `<span class="ticket-status-badge status-badge-claimed">🟡 Claimed by ${escapeHtml(t.claimed_by_name || 'Staff')}</span>`;
        } else if (t.status === 'closed') {
            statusBadge = '<span class="ticket-status-badge status-badge-closed">🔴 Closed</span>';
        }

        // Panel label
        const matchedPanel = currentPanels.find(p => p.id === t.panel_id);
        const panelName = matchedPanel ? matchedPanel.name : (t.panel_id ? `Panel #${t.panel_id}` : 'Main Desk');

        // Actions
        let actionButtons = `
            <button class="btn btn-secondary btn-sm" onclick="openTranscriptModal(${t.id})">📜 Transcript</button>
            <div class="dropdown d-inline-block">
                <button class="btn btn-secondary btn-sm" onclick="downloadTranscript(${t.id}, 'html')">📥 HTML</button>
                <button class="btn btn-secondary btn-sm" onclick="downloadTranscript(${t.id}, 'txt')">TXT</button>
            </div>
        `;

        if (t.status !== 'closed') {
            actionButtons += `
                <button class="btn btn-secondary btn-sm" onclick="openAssignModal(${t.id})">👥 Assign</button>
                <button class="btn btn-danger btn-sm" onclick="openCloseModal(${t.id})">🔒 Close</button>
            `;
            if (t.status === 'open') {
                actionButtons = `<button class="btn btn-primary btn-sm" onclick="claimTicket(${t.id})">🙋‍♂️ Claim</button> ` + actionButtons;
            }
        }

        tr.innerHTML = `
            <td><strong>#${String(t.id).padStart(4, '0')}</strong></td>
            <td>
                <div class="d-flex align-center gap-2">
                    <span class="user-avatar-placeholder">👤</span>
                    <div>
                        <div class="font-semibold">${escapeHtml(t.user_name)}</div>
                        <div class="text-xs text-muted">ID: ${t.user_id}</div>
                    </div>
                </div>
            </td>
            <td>
                <span class="badge" style="background: rgba(88,101,242,0.15); color: #5865F2; padding: 4px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; display: inline-flex; align-items: center; gap: 4px;">
                    🎫 ${escapeHtml(panelName)}
                </span>
            </td>
            <td><span class="tag-badge">${escapeHtml(t.category_name)}</span></td>
            <td>
                <div class="font-semibold">${escapeHtml(t.subject)}</div>
                <div class="text-xs text-muted text-truncate" style="max-width: 240px;">${escapeHtml(t.initial_details || '')}</div>
            </td>
            <td>${statusBadge}</td>
            <td class="text-xs text-muted">${escapeHtml(t.created_at || 'N/A')}</td>
            <td><div class="d-flex gap-1 flex-wrap">${actionButtons}</div></td>
        `;
        tbody.appendChild(tr);
    });
}

// Claim Ticket
async function claimTicket(ticketId) {
    const res = await apiRequest(`/api/guild/${window.guildId}/tickets/${ticketId}/claim`, 'POST', {});
    if (res && res.success) {
        await loadTicketsList();
    } else {
        alert(res?.error || 'Failed to claim ticket.');
    }
}

function filterAssignRoles(query = '') {
    const q = query.trim().toLowerCase();
    const rSelect = document.getElementById('assign-role-select');
    if (!rSelect) return;
    const curVal = rSelect.value;
    rSelect.innerHTML = '<option value="">-- Choose Role to Assign --</option>';
    serverRoles.forEach(r => {
        if (!q || r.name.toLowerCase().includes(q) || String(r.id).includes(q)) {
            const opt = document.createElement('option');
            opt.value = String(r.id);
            opt.textContent = `@${r.name}`;
            if (curVal && curVal === String(r.id)) opt.selected = true;
            rSelect.appendChild(opt);
        }
    });
}

function filterAssignMembers(query = '') {
    const q = query.trim().toLowerCase();
    const mSelect = document.getElementById('assign-member-select');
    if (!mSelect) return;
    const curVal = mSelect.value;
    mSelect.innerHTML = '<option value="">-- Choose Member to Add --</option>';
    serverMembers.forEach(m => {
        const name = (m.display_name || m.name || '').toLowerCase();
        const id = String(m.id);
        if (!q || name.includes(q) || id.includes(q)) {
            const opt = document.createElement('option');
            opt.value = String(m.id);
            opt.textContent = `${m.display_name || m.name} (${m.id})`;
            if (curVal && curVal === String(m.id)) opt.selected = true;
            mSelect.appendChild(opt);
        }
    });
}

// Assign Role / Member Modal
function openAssignModal(ticketId) {
    currentViewingTicketId = ticketId;
    document.getElementById('assign-modal-ticket-id').innerText = `#${String(ticketId).padStart(4, '0')}`;

    const rSearch = document.getElementById('assign-role-search');
    if (rSearch) rSearch.value = '';
    const mSearch = document.getElementById('assign-member-search');
    if (mSearch) mSearch.value = '';

    filterAssignRoles('');
    filterAssignMembers('');

    document.getElementById('modal-ticket-assign').style.display = 'flex';
}

function closeAssignModal() {
    document.getElementById('modal-ticket-assign').style.display = 'none';
}

async function submitAssignModal(event) {
    if (event) event.preventDefault();
    const roleId = document.getElementById('assign-role-select').value;
    const memberId = document.getElementById('assign-member-select').value;

    if (!roleId && !memberId) {
        alert('Please choose either a Role or Member to assign.');
        return;
    }

    const res = await apiRequest(`/api/guild/${window.guildId}/tickets/${currentViewingTicketId}/assign`, 'POST', {
        role_id: roleId || null,
        member_id: memberId || null
    });

    if (res && res.success) {
        closeAssignModal();
        alert('✅ Assigned successfully and added to private thread!');
        await loadTicketsList();
    } else {
        alert(res?.error || 'Failed to assign role or member.');
    }
}

// Close Ticket Modal
function openCloseModal(ticketId) {
    currentViewingTicketId = ticketId;
    document.getElementById('close-modal-ticket-id').innerText = `#${String(ticketId).padStart(4, '0')}`;
    document.getElementById('ticket-close-reason').value = '';
    document.getElementById('modal-ticket-close').style.display = 'flex';
}

function closeCloseModal() {
    document.getElementById('modal-ticket-close').style.display = 'none';
}

async function submitCloseModal(event) {
    if (event) event.preventDefault();
    const reason = document.getElementById('ticket-close-reason').value.trim() || 'Closed via Web Dashboard';

    const res = await apiRequest(`/api/guild/${window.guildId}/tickets/${currentViewingTicketId}/close`, 'POST', {
        reason: reason
    });

    if (res && res.success) {
        closeCloseModal();
        alert('🔒 Ticket successfully closed, transcript saved, and Discord thread archived.');
        await loadTicketsList();
    } else {
        alert(res?.error || 'Failed to close ticket.');
    }
}

// =============================================================================
// 7. Full Transcript Viewer Modal & Downloads
// =============================================================================

async function openTranscriptModal(ticketId) {
    currentViewingTicketId = ticketId;
    document.getElementById('transcript-modal-ticket-id').innerText = `#${String(ticketId).padStart(4, '0')}`;
    const chatBody = document.getElementById('transcript-chat-body');
    chatBody.innerHTML = '<div class="text-center text-muted p-4">Loading conversation transcript...</div>';
    document.getElementById('modal-ticket-transcript').style.display = 'flex';

    const res = await apiRequest(`/api/guild/${window.guildId}/tickets/${ticketId}/transcript`, 'GET');
    if (!res || !res.success) {
        chatBody.innerHTML = '<div class="text-center text-danger p-4">Failed to load conversation transcript.</div>';
        return;
    }

    const t = res.ticket;
    const msgs = res.messages || [];

    // Header metadata
    document.getElementById('transcript-meta-subject').innerText = t.subject;
    document.getElementById('transcript-meta-opener').innerText = `${t.user_name} (${t.user_id})`;
    document.getElementById('transcript-meta-status').innerText = (t.status || 'open').toUpperCase();
    document.getElementById('transcript-meta-category').innerText = t.category_name;

    chatBody.innerHTML = '';

    // Opening Card
    const initialBox = document.createElement('div');
    initialBox.className = 'chat-msg-row';
    initialBox.innerHTML = `
        <div class="chat-avatar" style="display:flex;align-items:center;justify-content:center;color:#fff;">🎫</div>
        <div class="chat-msg-content">
            <div class="chat-msg-header">
                <span class="chat-author">${escapeHtml(t.user_name)}</span>
                <span class="chat-badge" style="background:#4e5058;color:#fff;">CREATOR</span>
                <span class="chat-time">${escapeHtml(t.created_at || '')}</span>
            </div>
            <div class="chat-text" style="background:rgba(88,101,242,0.1);border-left:3px solid #5865f2;padding:10px 14px;border-radius:4px;">
                <strong>Subject:</strong> ${escapeHtml(t.subject)}<br><br>
                ${escapeHtml(t.initial_details || '')}
            </div>
        </div>
    `;
    chatBody.appendChild(initialBox);

    if (msgs.length === 0) {
        const emptyNotice = document.createElement('div');
        emptyNotice.className = 'text-center text-muted p-3';
        emptyNotice.innerText = 'No messages sent in this ticket yet.';
        chatBody.appendChild(emptyNotice);
        return;
    }

    msgs.forEach(m => {
        const row = document.createElement('div');
        row.className = 'chat-msg-row';

        const avatarUrl = m.author_avatar || 'https://cdn.discordapp.com/embed/avatars/0.png';
        const staffBadge = m.is_staff ? '<span class="chat-badge chat-badge-staff">STAFF</span>' : '';

        let attsHtml = '';
        if (m.attachments && m.attachments.length > 0) {
            attsHtml = '<div class="chat-attachment-grid">';
            m.attachments.forEach(url => {
                const isImg = /\.(png|jpe?g|webp|gif)(\?.*)?$/i.test(url);
                if (isImg) {
                    attsHtml += `<a href="${url}" target="_blank" rel="noopener noreferrer"><img class="chat-img-thumb" src="${url}" alt="Attachment"></a>`;
                } else {
                    const fname = url.split('/').pop().split('?')[0];
                    attsHtml += `<a href="${url}" target="_blank" rel="noopener noreferrer" class="tag-badge">📎 ${escapeHtml(fname)}</a>`;
                }
            });
            attsHtml += '</div>';
        }

        row.innerHTML = `
            <img class="chat-avatar" src="${avatarUrl}" alt="${escapeHtml(m.author_name)}" onerror="this.src='https://cdn.discordapp.com/embed/avatars/0.png'">
            <div class="chat-msg-content">
                <div class="chat-msg-header">
                    <span class="chat-author">${escapeHtml(m.author_name)}</span>
                    ${staffBadge}
                    <span class="chat-time">${escapeHtml(m.created_at || '')}</span>
                </div>
                <div class="chat-text">${escapeHtml(m.content || '')}</div>
                ${attsHtml}
            </div>
        `;
        chatBody.appendChild(row);
    });

    // Auto scroll to bottom
    chatBody.scrollTop = chatBody.scrollHeight;
}

function closeTranscriptModal() {
    document.getElementById('modal-ticket-transcript').style.display = 'none';
}

function downloadTranscript(ticketId, format = 'html') {
    const tId = ticketId || currentViewingTicketId;
    if (!tId) return;
    const url = `/api/guild/${window.guildId}/tickets/${tId}/transcript/download?format=${format}`;
    window.open(url, '_blank');
}

// Utility: HTML escape
function escapeHtml(text) {
    if (!text) return '';
    const map = {
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#039;'
    };
    return String(text).replace(/[&<>"']/g, m => map[m]);
}
