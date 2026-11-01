/**
 * inbox.js — Direct Messages 2-Column Messenger Dashboard Controller
 * Handles conversation browsing, search, active chat streaming,
 * real-time polling, replying via Discord bot, canned templates, and exports.
 */

let activeConvId = null;
let currentConversations = [];
let activeInboxFilter = 'all';
let inboxSearchTerm = '';
let inboxPollInterval = null;
let inboxBadgeInterval = null;
let activeConvData = null;

// Helper: escape HTML
function escapeInboxHtml(text) {
    if (text === null || text === undefined) return '';
    const map = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'};
    return String(text).replace(/[&<>"']/g, m => map[m]);
}

// Helper: relative time formatter
function formatInboxRelativeTime(dateStr) {
    if (!dateStr) return '';
    try {
        const d = new Date(dateStr.endsWith('Z') ? dateStr : dateStr + 'Z');
        const now = new Date();
        const diffMs = now - d;
        const diffSec = Math.floor(diffMs / 1000);
        if (diffSec < 60) return 'Just now';
        const diffMin = Math.floor(diffSec / 60);
        if (diffMin < 60) return `${diffMin}m ago`;
        const diffHr = Math.floor(diffMin / 60);
        if (diffHr < 24) return `${diffHr}h ago`;
        const diffDays = Math.floor(diffHr / 24);
        if (diffDays < 7) return `${diffDays}d ago`;
        return d.toLocaleDateString();
    } catch (e) {
        return dateStr;
    }
}

// =============================================================================
// Initialization
// =============================================================================

async function initInboxTab() {
    console.log("[Inbox] Initializing Direct Messages tab...");
    await loadConversations(true);
    startInboxSmartPolling();
}

function startInboxSmartPolling() {
    stopInboxSmartPolling();
    // Poll active chat every 4 seconds while user is on this tab
    inboxPollInterval = setInterval(async () => {
        const tab = document.getElementById('tab-inbox');
        if (tab && tab.classList.contains('active')) {
            await pollInboxUpdates();
        }
    }, 4000);
}

function stopInboxSmartPolling() {
    if (inboxPollInterval) {
        clearInterval(inboxPollInterval);
        inboxPollInterval = null;
    }
}

async function pollInboxUpdates() {
    if (!activeConvId) {
        await loadConversations(false);
        return;
    }
    try {
        const res = await apiRequest(`/api/guild/${window.guildId}/inbox/conversations/${activeConvId}`, 'GET');
        if (res && res.success) {
            renderChatMessages(res.messages, res.conversation);
        }
        await loadConversations(false);
    } catch (e) {
        console.warn("[Inbox] Poll error:", e);
    }
}

// =============================================================================
// Conversations List
// =============================================================================

async function loadConversations(autoSelectFirst = false) {
    const listContainer = document.getElementById('inbox-conv-list');
    if (!listContainer) return;

    try {
        const url = `/api/guild/${window.guildId}/inbox/conversations?status=${activeInboxFilter}&search=${encodeURIComponent(inboxSearchTerm)}`;
        const res = await apiRequest(url, 'GET');
        if (!res || !res.success) {
            listContainer.innerHTML = '<div class="text-muted text-center p-3 text-xs">Failed to load conversations.</div>';
            return;
        }

        currentConversations = res.conversations || [];
        updateUnreadBadges(res.unread_total || 0);

        if (currentConversations.length === 0) {
            listContainer.innerHTML = `
                <div class="text-muted text-center p-4 text-xs">
                    <p style="font-size: 1.5rem; margin-bottom: 6px;">📭</p>
                    No direct messages found.
                </div>
            `;
            if (autoSelectFirst) {
                renderEmptyChatState();
            }
            return;
        }

        let html = '';
        currentConversations.forEach(c => {
            const isActive = c.id === activeConvId ? 'active' : '';
            const avatar = c.avatar_url || 'https://cdn.discordapp.com/embed/avatars/0.png';
            const name = escapeInboxHtml(c.display_name || c.username);
            const timeStr = formatInboxRelativeTime(c.last_message_at);
            const preview = escapeInboxHtml(c.last_message_preview || 'No messages yet');
            const unreadBadge = c.unread_count > 0 ? `<span class="inbox-unread-count">${c.unread_count}</span>` : '';

            html += `
                <div class="inbox-conv-item ${isActive}" onclick="selectConversation(${c.id})" id="conv-item-${c.id}">
                    <div class="inbox-avatar-wrap">
                        <img src="${avatar}" class="inbox-avatar" alt="${name}" onerror="this.src='https://cdn.discordapp.com/embed/avatars/0.png'">
                    </div>
                    <div class="inbox-conv-details">
                        <div class="inbox-conv-top">
                            <span class="inbox-conv-name">${name}</span>
                            <span class="inbox-conv-time">${timeStr}</span>
                        </div>
                        <div class="inbox-conv-bottom">
                            <span class="inbox-conv-snippet">${preview}</span>
                            ${unreadBadge}
                        </div>
                    </div>
                </div>
            `;
        });

        listContainer.innerHTML = html;

        if (autoSelectFirst && currentConversations.length > 0 && !activeConvId) {
            selectConversation(currentConversations[0].id);
        }
    } catch (e) {
        console.error("[Inbox] loadConversations error:", e);
    }
}

function updateUnreadBadges(count) {
    const badge = document.getElementById('inbox-unread-badge');
    const headerCount = document.getElementById('inbox-header-unread-count');
    if (badge) {
        if (count > 0) {
            badge.style.display = 'inline-block';
            badge.textContent = count;
        } else {
            badge.style.display = 'none';
        }
    }
    if (headerCount) {
        headerCount.textContent = count;
    }
}

function handleInboxFilter(filter, btn) {
    activeInboxFilter = filter;
    document.querySelectorAll('.inbox-filter-btn').forEach(b => b.classList.remove('active'));
    if (btn) btn.classList.add('active');
    loadConversations(true);
}

let searchDebounceTimer = null;
function handleInboxSearch(val) {
    clearTimeout(searchDebounceTimer);
    searchDebounceTimer = setTimeout(() => {
        inboxSearchTerm = val.trim();
        loadConversations(false);
    }, 300);
}

// =============================================================================
// Active Conversation & Chat Stream
// =============================================================================

async function selectConversation(convId) {
    activeConvId = convId;

    // Highlight item in sidebar
    document.querySelectorAll('.inbox-conv-item').forEach(el => el.classList.remove('active'));
    const targetEl = document.getElementById(`conv-item-${convId}`);
    if (targetEl) targetEl.classList.add('active');

    try {
        const res = await apiRequest(`/api/guild/${window.guildId}/inbox/conversations/${convId}`, 'GET');
        if (res && res.success) {
            activeConvData = res.conversation;
            renderChatHeader(res.conversation);
            renderChatMessages(res.messages, res.conversation, true);
            // Re-render conversation item to clear unread pill
            const convObj = currentConversations.find(c => c.id === convId);
            if (convObj) convObj.unread_count = 0;
            const itemBadge = targetEl ? targetEl.querySelector('.inbox-unread-count') : null;
            if (itemBadge) itemBadge.remove();
        }
    } catch (e) {
        console.error("[Inbox] selectConversation error:", e);
    }
}

function renderChatHeader(conv) {
    const headerEl = document.getElementById('inbox-chat-header');
    if (!headerEl || !conv) return;

    const avatar = conv.avatar_url || 'https://cdn.discordapp.com/embed/avatars/0.png';
    const name = escapeInboxHtml(conv.display_name || conv.username);
    const username = escapeInboxHtml(conv.username);
    const userId = conv.user_id;
    const status = (conv.status || 'open').toLowerCase();
    const isClosed = status === 'closed';

    headerEl.innerHTML = `
        <div class="inbox-target-profile">
            <img src="${avatar}" class="inbox-target-avatar" alt="${name}" onerror="this.src='https://cdn.discordapp.com/embed/avatars/0.png'">
            <div>
                <div class="inbox-target-title">
                    <span>${name}</span>
                    <span class="text-xs text-muted">(@${username})</span>
                    <span class="inbox-target-badge ${isClosed ? 'badge-status-closed' : 'badge-status-open'}">${isClosed ? 'Closed' : 'Active'}</span>
                </div>
                <div class="inbox-target-subtitle">
                    <span>ID: <code>${userId}</code></span>
                    <span>•</span>
                    <a href="https://discord.com/users/${userId}" target="_blank" style="color:#5865F2; text-decoration:none;">View Profile ↗</a>
                </div>
            </div>
        </div>
        <div class="inbox-chat-actions">
            <button type="button" class="btn btn-secondary btn-sm" onclick="toggleConvStatus(${conv.id}, '${isClosed ? 'open' : 'closed'}')">
                ${isClosed ? '🔓 Reopen Thread' : '🔒 Close Thread'}
            </button>
            <button type="button" class="btn btn-secondary btn-sm" onclick="exportInboxTranscript(${conv.id}, 'html')" title="Download HTML Transcript">
                📥 Export
            </button>
        </div>
    `;

    // Enable composer
    const composerPane = document.getElementById('inbox-composer-pane');
    if (composerPane) composerPane.style.display = 'block';
}

function renderChatMessages(messages, conv, forceScroll = false) {
    const streamEl = document.getElementById('inbox-chat-stream');
    if (!streamEl) return;

    const isNearBottom = streamEl.scrollHeight - streamEl.scrollTop - streamEl.clientHeight < 120;

    if (!messages || messages.length === 0) {
        streamEl.innerHTML = `
            <div class="inbox-chat-empty">
                <div class="inbox-chat-empty-icon">💬</div>
                <h4>Start the Conversation</h4>
                <p class="text-xs text-muted">No messages in this direct message thread yet. Type a reply below.</p>
            </div>
        `;
        return;
    }

    let html = '';
    messages.forEach(m => {
        const isStaff = m.sender_type === 'staff_dashboard' || m.sender_type === 'staff_discord';
        const isAuto = m.sender_type === 'bot_auto';
        let wrapClass = 'inbox-msg-user';
        if (isStaff) wrapClass = 'inbox-msg-staff';
        if (isAuto) wrapClass = 'inbox-msg-auto';

        const authorName = escapeInboxHtml(m.author_name || (isStaff ? 'Staff' : 'Member'));
        const timeStr = formatInboxRelativeTime(m.sent_at);
        const content = escapeInboxHtml(m.content || '');

        let tagHtml = '';
        if (m.sender_type === 'staff_dashboard') {
            tagHtml = '<span class="inbox-staff-tag">Dashboard</span>';
        } else if (m.sender_type === 'staff_discord') {
            tagHtml = '<span class="inbox-discord-tag">#relay</span>';
        } else if (isAuto) {
            tagHtml = '<span class="inbox-staff-tag" style="background:#fee75c; color:#000;">Auto-Reply</span>';
        }

        // Attachments
        let attHtml = '';
        const attachments = m.attachments || [];
        if (attachments.length > 0) {
            attHtml += '<div class="inbox-msg-attachment">';
            attachments.forEach(att => {
                const url = escapeInboxHtml(att.url);
                const fname = escapeInboxHtml(att.filename || 'Attachment');
                const isImg = url.match(/\.(png|jpe?g|webp|gif)$/i);
                if (isImg) {
                    attHtml += `<a href="${url}" target="_blank"><img src="${url}" class="inbox-att-img" alt="${fname}"></a>`;
                } else {
                    attHtml += `<a href="${url}" target="_blank" class="inbox-att-card">📎 ${fname} ↗</a>`;
                }
            });
            attHtml += '</div>';
        }

        html += `
            <div class="inbox-msg-wrapper ${wrapClass}">
                <div class="inbox-msg-header">
                    <strong>${authorName}</strong>
                    ${tagHtml}
                    <span>•</span>
                    <small>${timeStr}</small>
                </div>
                <div class="inbox-msg-bubble">
                    <div class="inbox-msg-body">${content}</div>
                    ${attHtml}
                </div>
            </div>
        `;
    });

    streamEl.innerHTML = html;

    if (forceScroll || isNearBottom) {
        streamEl.scrollTop = streamEl.scrollHeight;
    }
}

function renderEmptyChatState() {
    activeConvId = null;
    activeConvData = null;
    const headerEl = document.getElementById('inbox-chat-header');
    if (headerEl) {
        headerEl.innerHTML = '<span class="text-muted text-sm">Select a conversation from the left to start messaging.</span>';
    }
    const streamEl = document.getElementById('inbox-chat-stream');
    if (streamEl) {
        streamEl.innerHTML = `
            <div class="inbox-chat-empty">
                <div class="inbox-chat-empty-icon">📥</div>
                <h3>Direct Messages Inbox</h3>
                <p class="text-xs text-muted">Select an existing member conversation on the left, or click "➕ New Direct Message" above to message any server member.</p>
            </div>
        `;
    }
    const composerPane = document.getElementById('inbox-composer-pane');
    if (composerPane) composerPane.style.display = 'none';
}

// =============================================================================
// Sending Staff Reply
// =============================================================================

async function sendInboxReply() {
    if (!activeConvId) return;
    const textarea = document.getElementById('inbox-reply-input');
    const sendBtn = document.getElementById('inbox-btn-send');
    if (!textarea || !sendBtn) return;

    const content = textarea.value.trim();
    if (!content) return;

    sendBtn.disabled = true;
    sendBtn.textContent = 'Sending...';

    try {
        const res = await apiRequest(`/api/guild/${window.guildId}/inbox/conversations/${activeConvId}/reply`, 'POST', {
            content: content
        });

        if (res && res.success) {
            textarea.value = '';
            textarea.style.height = 'auto';
            if (res.messages) {
                renderChatMessages(res.messages, activeConvData, true);
            }
            await loadConversations(false);
        } else {
            alert(res?.error || 'Failed to send direct message to member.');
        }
    } catch (e) {
        console.error("[Inbox] sendInboxReply error:", e);
        alert('An unexpected error occurred while sending direct message.');
    } finally {
        sendBtn.disabled = false;
        sendBtn.innerHTML = '<span>Send DM</span> <span>➤</span>';
    }
}

function handleComposerKeydown(event) {
    if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        sendInboxReply();
    }
}

function autoExpandTextarea(el) {
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 140) + 'px';
}

function insertCannedReply(text) {
    const textarea = document.getElementById('inbox-reply-input');
    if (!textarea) return;
    if (textarea.value) {
        textarea.value += ' ' + text;
    } else {
        textarea.value = text;
    }
    autoExpandTextarea(textarea);
    textarea.focus();
}

async function toggleConvStatus(convId, newStatus) {
    try {
        const res = await apiRequest(`/api/guild/${window.guildId}/inbox/conversations/${convId}/status`, 'POST', {
            status: newStatus
        });
        if (res && res.success) {
            await selectConversation(convId);
            await loadConversations(false);
        }
    } catch (e) {
        console.error("[Inbox] toggleConvStatus error:", e);
    }
}

function exportInboxTranscript(convId, format) {
    window.open(`/api/guild/${window.guildId}/inbox/conversations/${convId}/export?format=${format}`, '_blank');
}

// =============================================================================
// Modals: New Direct Message & Settings
// =============================================================================

function openNewDMModal() {
    const modal = document.getElementById('modal-inbox-new');
    if (modal) modal.classList.add('active');
    const inputId = document.getElementById('inbox-new-userid');
    if (inputId) {
        inputId.value = '';
        inputId.focus();
    }
    const inputMsg = document.getElementById('inbox-new-message');
    if (inputMsg) inputMsg.value = '';
}

function closeNewDMModal() {
    const modal = document.getElementById('modal-inbox-new');
    if (modal) modal.classList.remove('active');
}

async function submitNewDM(event) {
    event.preventDefault();
    const userIdInput = document.getElementById('inbox-new-userid');
    const msgInput = document.getElementById('inbox-new-message');
    if (!userIdInput || !msgInput) return;

    const userId = userIdInput.value.trim();
    const content = msgInput.value.trim();

    if (!userId || !content) {
        alert('Please specify both User ID and message content.');
        return;
    }

    const submitBtn = document.getElementById('inbox-btn-submit-new');
    if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = 'Sending...';
    }

    try {
        const res = await apiRequest(`/api/guild/${window.guildId}/inbox/send_new`, 'POST', {
            user_id: userId,
            content: content
        });

        if (res && res.success) {
            closeNewDMModal();
            await loadConversations(false);
            if (res.conversation_id) {
                await selectConversation(res.conversation_id);
            }
            alert('✅ Direct message sent to member successfully!');
        } else {
            alert(res?.error || 'Failed to send direct message to member.');
        }
    } catch (e) {
        alert('Error initiating direct message.');
    } finally {
        if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.textContent = 'Send Message';
        }
    }
}

// Inbox Settings Modal
async function openInboxSettingsModal() {
    const modal = document.getElementById('modal-inbox-settings');
    if (!modal) return;
    modal.classList.add('active');

    try {
        const res = await apiRequest(`/api/guild/${window.guildId}/inbox/settings`, 'GET');
        if (res && res.success && res.settings) {
            const s = res.settings;
            const toggle = document.getElementById('inbox-setting-autoreply-toggle');
            const text = document.getElementById('inbox-setting-autoreply-text');
            const channelSelect = document.getElementById('inbox-setting-channel-select');

            if (toggle) toggle.checked = !!s.auto_reply_enabled;
            if (text) text.value = s.auto_reply_text || '';

            // Populate text channels into select
            if (channelSelect && window.guildChannels) {
                let optHtml = '<option value="">-- No Discord Relay Channel --</option>';
                window.guildChannels.forEach(ch => {
                    const selected = String(ch.id) === String(s.notify_channel_id) ? 'selected' : '';
                    optHtml += `<option value="${ch.id}" ${selected}>#${escapeInboxHtml(ch.name)}</option>`;
                });
                channelSelect.innerHTML = optHtml;
            }
        }
    } catch (e) {
        console.error("[Inbox] openInboxSettingsModal error:", e);
    }
}

function closeInboxSettingsModal() {
    const modal = document.getElementById('modal-inbox-settings');
    if (modal) modal.classList.remove('active');
}

async function submitInboxSettings(event) {
    event.preventDefault();
    const toggle = document.getElementById('inbox-setting-autoreply-toggle');
    const text = document.getElementById('inbox-setting-autoreply-text');
    const channelSelect = document.getElementById('inbox-setting-channel-select');

    const data = {
        auto_reply_enabled: toggle ? toggle.checked : true,
        auto_reply_text: text ? text.value.trim() : '',
        notify_channel_id: channelSelect ? channelSelect.value : ''
    };

    try {
        const res = await apiRequest(`/api/guild/${window.guildId}/inbox/settings`, 'POST', data);
        if (res && res.success) {
            closeInboxSettingsModal();
            alert('✅ Inbox settings saved successfully!');
        } else {
            alert(res?.error || 'Failed to save settings');
        }
    } catch (e) {
        alert('Error saving inbox settings.');
    }
}

