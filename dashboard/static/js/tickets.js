/**
 * tickets.js — Support Ticket System (Private Threads) Dashboard Controller
 * Full-featured interactive manager for Discord.py 2.x Private Thread Tickets.
 * Ensures strict 64-bit string ID serialization throughout all operations.
 */

// Module state
let currentTicketSettings = null;
let currentTicketCategories = [];
let currentTicketsList = [];
let serverChannels = [];
let serverRoles = [];
let serverMembers = [];

let selectedGlobalRoles = new Set();
let selectedGlobalMembers = new Set();
let selectedCatRoles = new Set();
let selectedCatMembers = new Set();

let currentViewingTicketId = null;
let currentEditingCategoryId = null;

// =============================================================================
// 1. Initialization & Core Data Loading
// =============================================================================

async function initTicketsTab() {
    console.log("[Tickets] Initializing Support Tickets tab for guild:", window.guildId);
    await loadTicketSettingsAndMeta();
    await loadTicketCategories();
    await loadTicketsList();
}

async function loadTicketSettingsAndMeta() {
    const res = await apiRequest(`/api/guild/${window.guildId}/tickets/settings`, 'GET');
    if (!res || !res.success) {
        console.error("[Tickets] Failed to load settings:", res);
        return;
    }

    currentTicketSettings = res.settings || {};
    serverChannels = res.channels || [];
    serverRoles = res.roles || [];
    serverMembers = res.members || [];

    // Populate Channel Selectors
    populateChannelDropdown('ticket-config-panel-channel', currentTicketSettings.panel_channel_id);
    populateChannelDropdown('ticket-config-transcript-channel', currentTicketSettings.transcript_channel_id, true);

    // Populate Settings Inputs
    document.getElementById('ticket-config-panel-title').value = currentTicketSettings.panel_title || '🛡️ Official Support & Issue Desk';
    document.getElementById('ticket-config-panel-desc').value = currentTicketSettings.panel_description || 'Click a category button below to open a secure Private Thread.';
    const color = currentTicketSettings.panel_color || '#5865F2';
    document.getElementById('ticket-config-panel-color').value = color;
    document.getElementById('ticket-config-panel-color-hex').value = color;
    document.getElementById('ticket-config-max-open').value = currentTicketSettings.max_open_per_user || 2;

    // Initialize Global Support Tag Pickers
    selectedGlobalRoles = new Set((currentTicketSettings.support_role_ids || []).map(String));
    selectedGlobalMembers = new Set((currentTicketSettings.support_member_ids || []).map(String));
    renderGlobalRolesTags();
    renderGlobalMembersTags();

    // Update Statistics
    updateTicketStats(res.counts || {});

    // Update Live Preview
    updateDiscordPreview();

    // Initialize Searchable Pickers
    initSearchablePickers();
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

function setupSearchableTagPicker(inputId, dropdownId, getItemsFn, selectedSet, renderTagsCallback, isMember = false) {
    const input = document.getElementById(inputId);
    const dropdown = document.getElementById(dropdownId);
    if (!input || !dropdown) return;

    function renderList(query = '') {
        const items = getItemsFn() || [];
        dropdown.innerHTML = '';
        const q = query.trim().toLowerCase();

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
            dropdown.innerHTML = '<div class="p-3 text-xs text-muted text-center">No matching results found</div>';
            return;
        }

        filtered.forEach(item => {
            const isSelected = selectedSet.has(String(item.id));
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
                if (selectedSet.has(String(item.id))) {
                    selectedSet.delete(String(item.id));
                } else {
                    selectedSet.add(String(item.id));
                }
                renderTagsCallback();
                const nowSelected = selectedSet.has(String(item.id));
                row.className = 'tag-dropdown-item' + (nowSelected ? ' selected' : '');
                const check = row.querySelector('.tag-check');
                if (check) check.innerText = nowSelected ? '✅' : '➕';
                input.focus();
            };
            dropdown.appendChild(row);
        });
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
        () => serverRoles,
        selectedGlobalRoles,
        renderGlobalRolesTags,
        false
    );
    setupSearchableTagPicker(
        'ticket-global-members-search-input',
        'ticket-global-members-dropdown',
        () => serverMembers,
        selectedGlobalMembers,
        renderGlobalMembersTags,
        true
    );
    setupSearchableTagPicker(
        'cat-roles-search-input',
        'cat-roles-dropdown',
        () => serverRoles,
        selectedCatRoles,
        renderCatRolesTags,
        false
    );
    setupSearchableTagPicker(
        'cat-members-search-input',
        'cat-members-dropdown',
        () => serverMembers,
        selectedCatMembers,
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
    document.getElementById('ticket-config-panel-color').value = val;
    document.getElementById('ticket-config-panel-color-hex').value = val;
    updateDiscordPreview();
}

// =============================================================================
// 3. Save Settings & Publish Panel
// =============================================================================

async function saveTicketSettings() {
    const panelCh = document.getElementById('ticket-config-panel-channel').value;
    const transCh = document.getElementById('ticket-config-transcript-channel').value;
    const title = document.getElementById('ticket-config-panel-title').value.trim();
    const desc = document.getElementById('ticket-config-panel-desc').value.trim();
    const color = document.getElementById('ticket-config-panel-color-hex').value.trim();
    const maxOpen = parseInt(document.getElementById('ticket-config-max-open').value, 10) || 2;

    const payload = {
        panel_channel_id: panelCh || null,
        transcript_channel_id: transCh || null,
        support_role_ids: Array.from(selectedGlobalRoles),
        support_member_ids: Array.from(selectedGlobalMembers),
        panel_title: title,
        panel_description: desc,
        panel_color: color,
        max_open_per_user: maxOpen
    };

    const res = await apiRequest(`/api/guild/${window.guildId}/tickets/settings`, 'POST', payload);
    if (res && res.success) {
        currentTicketSettings = res.settings;
        updateDiscordPreview();
        alert('✅ Ticket configuration saved successfully!');
    } else {
        alert(res?.error || 'Failed to save ticket settings.');
    }
}

async function publishTicketPanel() {
    const panelCh = document.getElementById('ticket-config-panel-channel').value;
    if (!panelCh) {
        alert('⚠️ Please select and save a designated Ticket Panel Channel before publishing.');
        return;
    }

    if (!confirm('🚀 Publish / Update live Ticket Panel embed & category buttons to Discord now?')) {
        return;
    }

    // Save first to ensure Discord receives latest changes
    await saveTicketSettings();

    const res = await apiRequest(`/api/guild/${window.guildId}/tickets/publish`, 'POST', {});
    if (res && res.success) {
        alert(`✅ ${res.message || 'Ticket panel successfully updated on Discord!'}`);
    } else {
        alert(`❌ Failed to publish panel: ${res?.error || 'Unknown error'}`);
    }
}

// =============================================================================
// 4. Live Discord Panel Preview
// =============================================================================

function updateDiscordPreview() {
    const title = document.getElementById('ticket-config-panel-title')?.value || '🛡️ Official Support & Issue Desk';
    const desc = document.getElementById('ticket-config-panel-desc')?.value || 'Click a category button below to open a secure Private Thread.';
    const color = document.getElementById('ticket-config-panel-color-hex')?.value || '#5865F2';

    const pTitle = document.getElementById('discord-preview-title');
    const pDesc = document.getElementById('discord-preview-desc');
    const pEmbed = document.getElementById('discord-preview-embed');
    const pButtons = document.getElementById('discord-preview-buttons');

    if (pTitle) pTitle.innerText = title;
    if (pDesc) pDesc.innerText = desc;
    if (pEmbed) pEmbed.style.borderLeftColor = color;

    if (pButtons) {
        pButtons.innerHTML = '';
        if (currentTicketCategories.length === 0) {
            pButtons.innerHTML = '<span class="text-xs text-muted">No categories configured.</span>';
            return;
        }

        currentTicketCategories.forEach(cat => {
            const btn = document.createElement('div');
            const style = (cat.button_style || 'primary').toLowerCase();
            btn.className = `discord-btn-mock discord-btn-${style}`;
            btn.innerHTML = `<span>${cat.emoji || '🎫'}</span> <span>${escapeHtml(cat.name)}</span>`;
            pButtons.appendChild(btn);
        });
    }
}

// =============================================================================
// 5. Category Buttons Builder
// =============================================================================

async function loadTicketCategories() {
    const res = await apiRequest(`/api/guild/${window.guildId}/tickets/categories`, 'GET');
    if (!res || !res.success) {
        console.error("[Tickets] Failed to load categories:", res);
        return;
    }
    currentTicketCategories = res.categories || [];
    renderCategoriesList();
    updateDiscordPreview();
    populateCategoryFilterDropdown();
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

    document.getElementById('modal-ticket-category').style.display = 'flex';
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
        assigned_member_ids: Array.from(selectedCatMembers)
    };

    let res;
    if (currentEditingCategoryId) {
        res = await apiRequest(`/api/guild/${window.guildId}/tickets/categories/${currentEditingCategoryId}`, 'PUT', payload);
    } else {
        res = await apiRequest(`/api/guild/${window.guildId}/tickets/categories`, 'POST', payload);
    }

    if (res && res.success) {
        closeCategoryModal();
        await loadTicketCategories();
    } else {
        alert(res?.error || 'Failed to save category button.');
    }
}

async function deleteCategory(catId) {
    if (!confirm('Are you sure you want to delete this ticket category button?')) return;
    const res = await apiRequest(`/api/guild/${window.guildId}/tickets/categories/${catId}`, 'DELETE');
    if (res && res.success) {
        await loadTicketCategories();
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
        await loadTicketCategories();
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
    const status = document.getElementById('tickets-filter-status')?.value || 'all';
    const categoryId = document.getElementById('tickets-filter-category')?.value || '';
    const search = document.getElementById('tickets-search-input')?.value || '';

    const params = new URLSearchParams();
    if (status) params.append('status', status);
    if (categoryId) params.append('category_id', categoryId);
    if (search) params.append('search', search);

    const res = await apiRequest(`/api/guild/${window.guildId}/tickets?${params.toString()}`, 'GET');
    if (!res || !res.success) {
        console.error("[Tickets] Failed to load tickets:", res);
        return;
    }

    currentTicketsList = res.tickets || [];
    renderTicketsTable();
    updateTicketStats(res.counts || {});
}

function renderTicketsTable() {
    const tbody = document.getElementById('tickets-table-body');
    if (!tbody) return;

    tbody.innerHTML = '';
    if (currentTicketsList.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted p-4">No tickets matching the filter criteria.</td></tr>';
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
            <td><span class="tag-badge">${escapeHtml(t.category_name)}</span></td>
            <td>
                <div class="font-semibold">${escapeHtml(t.subject)}</div>
                <div class="text-xs text-muted text-truncate" style="max-width: 260px;">${escapeHtml(t.initial_details || '')}</div>
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
