/**
 * meetings.js — Meeting & Training Assistant Dashboard Controller
 * Full-featured interactive manager for Discord Meeting Assistant Bot.
 * Handles live recording controls, settings, meeting history, and minutes viewer.
 */

// Module State
let currentMeetingSettings = null;
let currentMeetingList = [];
let activeRecordingState = null;
let meetingTimerInterval = null;
let meetingVoiceChannels = [];
let meetingTextChannels = [];
let meetingRoles = [];
let currentMeetingPage = 1;
let currentMeetingStatusFilter = '';

// Safe HTML escape helper
function escapeHtmlMtg(text) {
    if (text === null || text === undefined) return '';
    const str = String(text);
    const map = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'};
    return str.replace(/[&<>"']/g, m => map[m]);
}

function formatDurationMtg(seconds) {
    if (!seconds || seconds <= 0) return '0s';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    if (h > 0) {
        return `${h}h ${m}m ${s}s`;
    }
    if (m > 0) {
        return `${m}m ${s}s`;
    }
    return `${s}s`;
}

function formatTimerClock(seconds) {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    const pad = (n) => String(n).padStart(2, '0');
    if (h > 0) {
        return `${pad(h)}:${pad(m)}:${pad(s)}`;
    }
    return `${pad(m)}:${pad(s)}`;
}

// Safe Guild ID resolver
function getGuildId() {
    if (window.guildId) return String(window.guildId);
    const parts = window.location.pathname.split('/');
    for (let i = 0; i < parts.length; i++) {
        if (parts[i] === 'guild' && parts[i + 1]) {
            return parts[i + 1];
        }
    }
    return '';
}

// =============================================================================
// 1. Initialization
// =============================================================================

async function initMeetingsTab() {
    const gid = getGuildId();
    console.log("[Meetings] Initializing Meetings & Training tab for guild:", gid);
    try {
        await Promise.all([
            fetchMeetingStatus(),
            fetchMeetingChannels(),
            fetchMeetingSettings(),
            loadMeetingsList(1)
        ]);
    } catch (err) {
        console.error("[Meetings] Error during initialization:", err);
    }
}

// =============================================================================
// 2. Fetch Status, Channels & Settings
// =============================================================================

async function fetchMeetingStatus() {
    const gid = getGuildId();
    try {
        const res = await fetch(`/api/meetings/guilds/${gid}/status`);
        const data = await res.json();
        if (!data.success) return;

        // Update Bot Status Badge
        const badge = document.getElementById('mtg-bot-badge');
        const botNameEl = document.getElementById('mtg-bot-name');
        if (badge && botNameEl) {
            if (data.bot_online) {
                badge.className = 'meetings-status-badge online';
                badge.innerHTML = `<span class="meetings-dot"></span> Online`;
                botNameEl.textContent = data.bot_user || 'Meeting Bot';
            } else {
                badge.className = 'meetings-status-badge offline';
                badge.innerHTML = `<span class="meetings-dot"></span> Offline`;
                botNameEl.textContent = 'Token Not Configured or Bot Disconnected';
            }
        }

        // Update Stats Counters
        if (data.stats) {
            const elTotal = document.getElementById('mtg-stat-total');
            const elActive = document.getElementById('mtg-stat-active');
            const elCompleted = document.getElementById('mtg-stat-completed');
            const elParticipants = document.getElementById('mtg-stat-participants');
            if (elTotal) elTotal.textContent = data.stats.total_meetings || 0;
            if (elActive) elActive.textContent = data.stats.active_recordings || 0;
            if (elCompleted) elCompleted.textContent = data.stats.completed_meetings || 0;
            if (elParticipants) elParticipants.textContent = data.stats.unique_participants || 0;
        }

        // Active Meeting State
        renderActiveMeetingState(data.active_meeting);

    } catch (err) {
        console.error("[Meetings] Failed to fetch status:", err);
    }
}

async function fetchMeetingChannels() {
    const gid = getGuildId();
    try {
        const res = await fetch(`/api/meetings/guilds/${gid}/channels`);
        const data = await res.json();
        if (data.success) {
            meetingVoiceChannels = data.voice_channels || [];
            meetingTextChannels = data.text_channels || [];
            meetingRoles = data.roles || [];
            populateChannelDropdowns();
        }
    } catch (err) {
        console.error("[Meetings] Failed to fetch channels:", err);
    }
}

function populateChannelDropdowns() {
    // Populate start modal voice channels
    const vcSelect = document.getElementById('mtg-start-voice-channel');
    if (vcSelect) {
        vcSelect.innerHTML = '<option value="">-- Select Voice Channel --</option>';
        meetingVoiceChannels.forEach(c => {
            const opt = document.createElement('option');
            opt.value = c.id;
            opt.textContent = `🔊 ${c.name} (${c.member_count} connected)`;
            vcSelect.appendChild(opt);
        });
    }

    // Populate start modal output channels
    const outSelect = document.getElementById('mtg-start-output-channel');
    if (outSelect) {
        outSelect.innerHTML = '<option value="">-- Use Default Output Channel --</option>';
        meetingTextChannels.forEach(c => {
            const opt = document.createElement('option');
            opt.value = c.id;
            opt.textContent = c.label || `#${c.name}`;
            outSelect.appendChild(opt);
        });
    }

    // Populate settings default output channel
    const defaultOutSelect = document.getElementById('mtg-setting-output-channel');
    if (defaultOutSelect) {
        defaultOutSelect.innerHTML = '<option value="">-- None (Must specify per meeting) --</option>';
        meetingTextChannels.forEach(c => {
            const opt = document.createElement('option');
            opt.value = c.id;
            opt.textContent = c.label || `#${c.name}`;
            defaultOutSelect.appendChild(opt);
        });
        if (currentMeetingSettings && currentMeetingSettings.default_output_channel_id) {
            defaultOutSelect.value = currentMeetingSettings.default_output_channel_id;
        }
    }

    // Populate settings allowed roles
    const rolesContainer = document.getElementById('mtg-setting-roles-container');
    if (rolesContainer) {
        rolesContainer.innerHTML = '';
        if (!meetingRoles || meetingRoles.length === 0) {
            rolesContainer.innerHTML = '<span style="font-size: 12px; color: var(--text-muted);">No roles found or bot not connected to server.</span>';
        } else {
            const allowedSet = new Set(currentMeetingSettings ? currentMeetingSettings.allowed_role_ids : []);
            meetingRoles.forEach(r => {
                const label = document.createElement('label');
                label.style.display = 'inline-flex';
                label.style.alignItems = 'center';
                label.style.gap = '6px';
                label.style.marginRight = '12px';
                label.style.marginBottom = '6px';
                label.style.fontSize = '12px';
                label.style.cursor = 'pointer';

                const chk = document.createElement('input');
                chk.type = 'checkbox';
                chk.value = r.id;
                chk.className = 'mtg-role-checkbox';
                if (allowedSet.has(r.id)) chk.checked = true;

                label.appendChild(chk);
                label.appendChild(document.createTextNode(r.name));
                rolesContainer.appendChild(label);
            });
        }
    }
}

async function fetchMeetingSettings() {
    const gid = getGuildId();
    try {
        const res = await fetch(`/api/meetings/guilds/${gid}/settings`);
        const data = await res.json();
        if (data.success && data.settings) {
            currentMeetingSettings = data.settings;
            const defaultOutSelect = document.getElementById('mtg-setting-output-channel');
            if (defaultOutSelect && currentMeetingSettings.default_output_channel_id) {
                defaultOutSelect.value = currentMeetingSettings.default_output_channel_id;
            }
        }
    } catch (err) {
        console.error("[Meetings] Failed to fetch settings:", err);
    }
}

async function saveMeetingSettings() {
    const gid = getGuildId();
    const outSelect = document.getElementById('mtg-setting-output-channel');
    const defaultOutId = outSelect ? outSelect.value : null;

    const roleCheckboxes = document.querySelectorAll('.mtg-role-checkbox:checked');
    const allowedRoles = Array.from(roleCheckboxes).map(cb => cb.value);

    try {
        const res = await fetch(`/api/meetings/guilds/${gid}/settings`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                default_output_channel_id: defaultOutId || null,
                allowed_role_ids: allowedRoles
            })
        });
        const data = await res.json();
        if (data.success) {
            alert("✅ Meeting Assistant settings updated successfully.");
            currentMeetingSettings = {
                guild_id: gid,
                default_output_channel_id: defaultOutId,
                allowed_role_ids: allowedRoles
            };
        } else {
            alert("❌ Failed to save settings: " + (data.error || "Unknown error"));
        }
    } catch (err) {
        alert("❌ Error saving settings: " + err);
    }
}

// =============================================================================
// 3. Live Active Meeting Recording Banner
// =============================================================================

function renderActiveMeetingState(activeMeeting) {
    activeRecordingState = activeMeeting;
    const banner = document.getElementById('mtg-active-banner');
    if (!banner) return;

    if (meetingTimerInterval) {
        clearInterval(meetingTimerInterval);
        meetingTimerInterval = null;
    }

    if (!activeMeeting) {
        banner.style.display = 'none';
        return;
    }

    banner.style.display = 'flex';
    const titleEl = document.getElementById('mtg-active-title');
    const metaEl = document.getElementById('mtg-active-meta');
    const timerEl = document.getElementById('mtg-active-timer');

    if (titleEl) titleEl.textContent = activeMeeting.title || 'Live Meeting';
    if (metaEl) {
        metaEl.innerHTML = `
            <span>🔊 Channel: <strong>${escapeHtmlMtg(activeMeeting.voice_channel_name)}</strong></span>
            <span>🆔 ID: <strong>#${activeMeeting.meeting_id}</strong></span>
        `;
    }

    // Start live stopwatch
    let elapsed = activeMeeting.elapsed_seconds || 0;
    if (timerEl) timerEl.textContent = formatTimerClock(elapsed);

    meetingTimerInterval = setInterval(() => {
        elapsed += 1;
        if (timerEl) timerEl.textContent = formatTimerClock(elapsed);
    }, 1000);
}

async function stopActiveMeetingFromUI() {
    if (!activeRecordingState) return;
    const meetingId = activeRecordingState.meeting_id;
    if (!confirm(`Are you sure you want to stop recording "${activeRecordingState.title}"?\n\nThe bot will disconnect from voice, compress audio, and generate structured meeting minutes via Gemini AI.`)) {
        return;
    }

    const btn = document.getElementById('mtg-btn-stop-active');
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `⏳ Processing...`;
    }

    const gid = getGuildId();
    try {
        const res = await fetch(`/api/meetings/guilds/${gid}/stop`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ meeting_id: meetingId })
        });
        const data = await res.json();
        if (data.success) {
            alert("✅ " + data.message);
            renderActiveMeetingState(null);
            await fetchMeetingStatus();
            await loadMeetingsList(1);
        } else {
            alert("❌ Failed to stop meeting: " + (data.error || "Unknown error"));
        }
    } catch (err) {
        alert("❌ Error stopping meeting: " + err);
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = `⏹️ Stop Recording & Generate Minutes`;
        }
    }
}

// =============================================================================
// 4. Start New Meeting Recording Modal
// =============================================================================

function openStartMeetingModal() {
    const modal = document.getElementById('mtg-start-modal');
    if (modal) modal.style.display = 'flex';
}

function closeStartMeetingModal() {
    const modal = document.getElementById('mtg-start-modal');
    if (modal) modal.style.display = 'none';
}

async function submitStartMeeting(e) {
    if (e) e.preventDefault();
    const title = document.getElementById('mtg-start-title').value.trim();
    const vcId = document.getElementById('mtg-start-voice-channel').value;
    const outId = document.getElementById('mtg-start-output-channel').value;
    const projectCode = document.getElementById('mtg-start-project').value.trim();

    if (!title) {
        alert("Please enter a meeting title.");
        return;
    }
    if (!vcId) {
        alert("Please select a voice channel.");
        return;
    }

    const submitBtn = document.getElementById('mtg-btn-submit-start');
    if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = "Connecting to Voice...";
    }

    const gid = getGuildId();
    try {
        const res = await fetch(`/api/meetings/guilds/${gid}/start`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                title: title,
                voice_channel_id: vcId,
                output_channel_id: outId || null,
                project_code: projectCode || null
            })
        });
        const data = await res.json();
        if (data.success) {
            alert("🔴 Recording started successfully! The Meeting Bot has joined the voice channel.");
            closeStartMeetingModal();
            // Reset form
            document.getElementById('mtg-start-title').value = '';
            document.getElementById('mtg-start-project').value = '';
            await fetchMeetingStatus();
            await loadMeetingsList(1);
        } else {
            alert("❌ Failed to start recording: " + (data.error || "Unknown error"));
        }
    } catch (err) {
        alert("❌ Error starting recording: " + err);
    } finally {
        if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.textContent = "Start Recording";
        }
    }
}

// =============================================================================
// 5. Meeting History Table & Pagination
// =============================================================================

async function loadMeetingsList(page = 1) {
    currentMeetingPage = page;
    const tbody = document.getElementById('mtg-history-tbody');
    if (!tbody) return;

    tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; padding: 24px; color: var(--text-muted);">⏳ Loading meetings...</td></tr>`;

    const statusFilter = document.getElementById('mtg-filter-status') ? document.getElementById('mtg-filter-status').value : '';
    currentMeetingStatusFilter = statusFilter;

    const gid = getGuildId();
    let url = `/api/meetings/guilds/${gid}/list?page=${page}&limit=15`;
    if (statusFilter) url += `&status=${encodeURIComponent(statusFilter)}`;

    try {
        const res = await fetch(url);
        const data = await res.json();
        if (!data.success) {
            tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; color: #f87171;">Failed to load meetings.</td></tr>`;
            return;
        }

        currentMeetingList = data.meetings || [];
        renderMeetingsTable(currentMeetingList, data.total, page, data.limit);

    } catch (err) {
        tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; color: #f87171;">Error loading meetings: ${err}</td></tr>`;
    }
}

function renderMeetingsTable(meetings, total, page, limit) {
    const tbody = document.getElementById('mtg-history-tbody');
    if (!tbody) return;

    if (!meetings || meetings.length === 0) {
        tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; padding: 32px; color: var(--text-muted);">No meetings found. Start a recording to see minutes here!</td></tr>`;
        renderMeetingPagination(0, page, limit);
        return;
    }

    tbody.innerHTML = meetings.map(m => {
        const started = m.started_at ? m.started_at.replace('T', ' ').slice(0, 16) : 'N/A';
        const dur = formatDurationMtg(m.duration_seconds);
        const statusClass = (m.status || 'recording').toLowerCase();

        const projectBadge = m.project_code
            ? `<span class="meeting-project-tag">${escapeHtmlMtg(m.project_code)}</span>`
            : '';

        return `
            <tr>
                <td style="font-weight: 600; color: #94a3b8;">#${m.id}</td>
                <td>
                    <div style="font-weight: 600; color: #ffffff;">${escapeHtmlMtg(m.title)}</div>
                    <div style="margin-top: 2px;">${projectBadge}</div>
                </td>
                <td style="color: #94a3b8; font-size: 12px;">${started}</td>
                <td>🔊 ${escapeHtmlMtg(m.voice_channel_name)}</td>
                <td style="font-family: monospace;">${dur}</td>
                <td>
                    <span class="meeting-status-tag ${statusClass}">
                        ${escapeHtmlMtg(m.status)}
                    </span>
                </td>
                <td>
                    <div style="display: flex; gap: 6px;">
                        <button class="btn btn-sm btn-secondary" onclick="viewMeetingDetails(${m.id})" title="View Minutes & Transcript">
                            📋 Minutes
                        </button>
                        <a href="/api/meetings/${m.id}/export/markdown" class="btn btn-sm btn-secondary" title="Download Markdown Minutes">
                            📥 .md
                        </a>
                        <button class="btn btn-sm btn-danger" onclick="deleteMeetingRecord(${m.id}, '${escapeHtmlMtg(m.title)}')" title="Delete Record">
                            🗑️
                        </button>
                    </div>
                </td>
            </tr>
        `;
    }).join('');

    renderMeetingPagination(total, page, limit);
}

function renderMeetingPagination(total, page, limit) {
    const container = document.getElementById('mtg-pagination');
    if (!container) return;

    const totalPages = Math.ceil(total / limit);
    if (totalPages <= 1) {
        container.innerHTML = `<span style="font-size: 12px; color: var(--text-muted);">Total: ${total} meetings</span>`;
        return;
    }

    container.innerHTML = `
        <div style="display: flex; align-items: center; gap: 8px;">
            <button class="btn btn-sm btn-secondary" ${page <= 1 ? 'disabled' : ''} onclick="loadMeetingsList(${page - 1})">
                ◀ Prev
            </button>
            <span style="font-size: 13px; color: #cbd5e1;">Page ${page} of ${totalPages} (${total} total)</span>
            <button class="btn btn-sm btn-secondary" ${page >= totalPages ? 'disabled' : ''} onclick="loadMeetingsList(${page + 1})">
                Next ▶
            </button>
        </div>
    `;
}

// =============================================================================
// 6. View Meeting Minutes Modal
// =============================================================================

async function viewMeetingDetails(meetingId) {
    const modal = document.getElementById('mtg-minutes-modal');
    const body = document.getElementById('mtg-minutes-modal-body');
    if (!modal || !body) return;

    modal.style.display = 'flex';
    body.innerHTML = `<div style="text-align: center; padding: 40px; color: var(--text-muted);">⏳ Loading meeting minutes...</div>`;

    try {
        const res = await fetch(`/api/meetings/${meetingId}`);
        const data = await res.json();
        if (!data.success) {
            body.innerHTML = `<div style="color: #f87171; padding: 20px;">Failed to load meeting details: ${data.error}</div>`;
            return;
        }

        const m = data.meeting;
        const participants = data.participants || [];

        // Set modal title & download links
        const titleEl = document.getElementById('mtg-minutes-title');
        if (titleEl) titleEl.textContent = `📋 Minutes: ${m.title}`;

        const dlMd = document.getElementById('mtg-btn-dl-md');
        if (dlMd) dlMd.href = `/api/meetings/${m.id}/export/markdown`;
        const dlTxt = document.getElementById('mtg-btn-dl-txt');
        if (dlTxt) dlTxt.href = `/api/meetings/${m.id}/export/transcript`;

        // Render Key Points
        const keyPointsHtml = (m.key_points && m.key_points.length > 0)
            ? `<ul class="mtg-list-bullet">${m.key_points.map(p => `<li>${escapeHtmlMtg(p)}</li>`).join('')}</ul>`
            : `<em style="color: var(--text-muted);">No key points recorded.</em>`;

        // Render Action Items
        const actionItemsHtml = (m.action_items && m.action_items.length > 0)
            ? m.action_items.map(a => `<div class="mtg-action-item"><span>☑️</span><span>${escapeHtmlMtg(a)}</span></div>`).join('')
            : `<em style="color: var(--text-muted);">No action items recorded.</em>`;

        // Render Q&A Decisions
        const qaHtml = (m.qa_decisions && m.qa_decisions.length > 0)
            ? `<ul class="mtg-list-bullet">${m.qa_decisions.map(q => `<li>${escapeHtmlMtg(q)}</li>`).join('')}</ul>`
            : `<em style="color: var(--text-muted);">No decisions recorded.</em>`;

        // Render Attendance Table
        const attendanceRows = participants.length > 0
            ? participants.map(p => {
                const j = p.joined_at ? p.joined_at.slice(11, 19) : 'N/A';
                const l = p.left_at ? p.left_at.slice(11, 19) : 'End of Meeting';
                return `
                    <tr>
                        <td style="font-weight: 600;">${escapeHtmlMtg(p.user_name)}</td>
                        <td>${j}</td>
                        <td>${l}</td>
                        <td style="font-family: monospace;">${p.duration_formatted || formatDurationMtg(p.total_seconds)}</td>
                    </tr>
                `;
            }).join('')
            : `<tr><td colspan="4" style="text-align: center; color: var(--text-muted);">No participant records found.</td></tr>`;

        // Build Full HTML
        body.innerHTML = `
            <!-- Overview Info Strip -->
            <div style="display: flex; gap: 16px; margin-bottom: 20px; flex-wrap: wrap; font-size: 13px; color: #cbd5e1; background: rgba(255,255,255,0.02); padding: 12px 16px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.06);">
                <div><strong>Host:</strong> ${escapeHtmlMtg(m.host_name)}</div>
                <div><strong>Channel:</strong> 🔊 ${escapeHtmlMtg(m.voice_channel_name)}</div>
                <div><strong>Duration:</strong> ⏱️ ${m.duration_formatted}</div>
                <div><strong>Participants:</strong> 👥 ${participants.length}</div>
                ${m.project_code ? `<div><strong>Project:</strong> <span class="meeting-project-tag">${escapeHtmlMtg(m.project_code)}</span></div>` : ''}
            </div>

            <!-- Executive Summary -->
            <div class="mtg-section-card">
                <div class="mtg-section-title">📝 Executive Summary</div>
                <div style="font-size: 13px; line-height: 1.6; color: #e2e8f0; white-space: pre-wrap;">
                    ${escapeHtmlMtg(m.executive_summary || 'No summary available.')}
                </div>
            </div>

            <!-- Key Discussion Points -->
            <div class="mtg-section-card">
                <div class="mtg-section-title">🔑 Key Discussion Points</div>
                ${keyPointsHtml}
            </div>

            <!-- Action Items -->
            <div class="mtg-section-card">
                <div class="mtg-section-title">✅ Action Items</div>
                ${actionItemsHtml}
            </div>

            <!-- Q&A and Decisions -->
            <div class="mtg-section-card">
                <div class="mtg-section-title">💬 Q&A & Decisions</div>
                ${qaHtml}
            </div>

            <!-- Attendance Report -->
            <div class="mtg-section-card">
                <div class="mtg-section-title">👥 Attendance Report (${participants.length})</div>
                <table class="mtg-attendance-table">
                    <thead>
                        <tr>
                            <th>Participant</th>
                            <th>Joined</th>
                            <th>Left</th>
                            <th>Duration</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${attendanceRows}
                    </tbody>
                </table>
            </div>

            <!-- Full Transcript -->
            <div class="mtg-section-card">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                    <div class="mtg-section-title" style="margin-bottom: 0;">📜 Full Transcript</div>
                    <button class="btn btn-sm btn-secondary" onclick="copyMtgTranscript()">📋 Copy Transcript</button>
                </div>
                <div class="mtg-transcript-box" id="mtg-transcript-text">${escapeHtmlMtg(m.full_transcript || 'No transcript generated.')}</div>
            </div>
        `;

    } catch (err) {
        body.innerHTML = `<div style="color: #f87171; padding: 20px;">Error loading meeting: ${err}</div>`;
    }
}

function closeMeetingMinutesModal() {
    const modal = document.getElementById('mtg-minutes-modal');
    if (modal) modal.style.display = 'none';
}

function copyMtgTranscript() {
    const el = document.getElementById('mtg-transcript-text');
    if (!el) return;
    navigator.clipboard.writeText(el.textContent).then(() => {
        alert("✅ Transcript copied to clipboard!");
    }).catch(err => {
        alert("Failed to copy transcript: " + err);
    });
}

// =============================================================================
// 7. Delete Meeting Record
// =============================================================================

async function deleteMeetingRecord(meetingId, title) {
    if (!confirm(`Are you sure you want to delete meeting #${meetingId} "${title}"?\n\nThis will permanently remove the minutes and attendance logs.`)) {
        return;
    }

    try {
        const res = await fetch(`/api/meetings/${meetingId}`, {
            method: 'DELETE'
        });
        const data = await res.json();
        if (data.success) {
            alert("✅ Meeting deleted.");
            await fetchMeetingStatus();
            await loadMeetingsList(currentMeetingPage);
        } else {
            alert("❌ Failed to delete: " + (data.error || "Unknown error"));
        }
    } catch (err) {
        alert("❌ Error deleting meeting: " + err);
    }
}
