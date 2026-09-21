function generateUUID() {
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function(c) {
        var r = Math.random() * 16 | 0, v = c === "x" ? r : (r & 0x3 | 0x8);
        return v.toString(16);
    });
}

function athenaApp() {
    return {
        // --- navigation ---
        currentPage: "chat",
        bots: [],
        activeBotId: null,
        activeRoomId: null,
        roomMessages: [],
        botMenuOpen: null,
        botModalOpen: false,
        botModalEditingId: null,
        botDraft: {name: "", model: "", selectedEndpointId: "", description: ""},
        vllmWarningOpen: false,
        vllmWarnedBotDraft: false,
        botEndpointPopupOpen: false,
        botModelPopupOpen: false,
        pickerTarget: "bot",
        agentDefaultsModalOpen: false,
        agentDefaultsDraft: {selectedEndpointId: "", model: "", naming_guidance: ""},
        athenaAgentModel: "",
        athenaAgentModelLabel: "Choose model",
        athenaAgentModelEndpointUrl: "",
        athenaAgentModelProvider: "",
        athenaAgentModelApiKey: "",
        modelPopupTarget: "main",
        athenaMenuOpen: null,
        activeRoom: null,
        groupRooms: [],
        groupModalOpen: false,
        groupDraft: {label: "", memberBotIds: []},
        mentionPopupOpen: false,
        mentionQuery: "",
        mentionStartIndex: null,
        mentionActiveIndex: 0,
        botToastMessage: "",
        botToastTimer: null,
        activeAthenaAgent: false,
        botComposerText: "",
        botConversationLoading: false,
        pages: [
            {id: "chat", label: "Chat", icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>'},
            {id: "bots", label: "Bots", icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="9" width="16" height="11" rx="2"/><path d="M12 9V5"/><circle cx="12" cy="3" r="1"/><circle cx="9" cy="14" r="1"/><circle cx="15" cy="14" r="1"/><path d="M4 14H2"/><path d="M22 14h-2"/></svg>'},
            {id: "memory", label: "Memory", icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9.5 2A2.5 2.5 0 0 1 12 4.5v15a2.5 2.5 0 0 1-4.96.5H7a2.5 2.5 0 0 1 0-5H8m4-11A2.5 2.5 0 0 1 14.5 2h.01A2.5 2.5 0 0 1 17 4.5v15a2.5 2.5 0 0 1-4.96.5"/></svg>'},
            {id: "skills", label: "Skills", icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2l2.4 7.2H22l-6 4.6 2.3 7.2-6.3-4.6-6.3 4.6 2.3-7.2-6-4.6h7.6z"/></svg>'},
            {id: "tasks", label: "Tasks", icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>'},
            {id: "notes", label: "Notes", icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z"/></svg>'},
            {id: "models", label: "Models", icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="8" rx="2"/><rect x="2" y="13" width="20" height="8" rx="2"/><line x1="6" y1="7" x2="6.01" y2="7"/><line x1="6" y1="17" x2="6.01" y2="17"/></svg>'},
            {id: "mcp", label: "MCP", icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 2v6"/><path d="M15 2v6"/><path d="M12 17v5"/><path d="M6 8h12l-1 6a5 5 0 0 1-10 0z"/></svg>'},
            {id: "settings", label: "Settings", icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>'},
        ],

        // --- theme ---
        theme: localStorage.getItem("athena_theme") || "light",

        // --- chat / sessions ---
        sessionId: localStorage.getItem("athena_session") || generateUUID(),
        sessions: [],
        sessionMenuOpen: null,
        expandedForkSessions: {},
        messages: [],
        inputText: "",
        sending: false,
        modelAliases: {},
        renamingModelValue: null,
        renameInputText: "",
        notePreviewMode: false,
        pinnedMenuOpen: false,
        backgroundGenerating: false,
        tasks: [],
        taskCapabilities: {tools: [], default_enabled_tools: [], skills: []},
        taskModalOpen: false,
        taskDraft: null,
        editingTaskId: null,
        taskModalError: '',
        deleteTaskConfirmId: null,
        historyReady: true,
        _streamBuffer: "",
        _streamFlushTimer: null,
        currentAbortController: null,
        isRecording: false,
        mediaRecorder: null,
        audioChunks: [],
        defaultModel: null,
        searchUrl: "",
        delegationConcurrency: 1,
        botPtcEnabled: false,
        asyncDelegationEnabled: false,
        layaGatingEnabled: false,
        model: "",
        modelLabel: "Select a model",
        modelEndpointUrl: "",
        modelProvider: "",
        modelApiKey: "",
        endpoints: [],
        addEndpointOpen: false,
        newEndpointType: "local",
        newEndpointName: "",
        newEndpointUrl: "",
        newEndpointProvider: "openai",
        newEndpointApiKey: "",
        maxCtx: 0,
        maxCtxText: "0",
        modelPopupOpen: false,
        workspace: "",
        workspacePopupOpen: false,
        workspaceBrowsePath: "",
        workspaceBrowseParent: null,
        workspaceBrowseDirs: [],
        newFolderName: "",
        fileBrowserOpen: false,
        fileBrowserPath: "",
        fileBrowserParent: null,
        fileBrowserEntries: [],
        filePreviewOpen: false,
        filePreviewPath: "",
        filePreviewContent: "",
        pendingImages: [],
        pendingAttachments: [],
        attachMenuOpen: false,
        mobileNavOpen: false,
        mobileSettingsSheetOpen: false,
        memoryExtractionModel: "",
        customTheme: null,
        themeSeedColor: "#7c3aed",
        colorPickerOpen: false,
        mcpServers: [],
        skillsTab: "skills",
        skills: [],
        skillFormOpen: false,
        skillDraft: {name: "", description: "", body: "", folder: null},
        skillUploadError: "",
        saveNoteFilePopupOpen: false,
        saveNoteBrowsePath: "",
        saveNoteBrowseParent: null,
        saveNoteBrowseDirs: [],
        saveNoteBrowseFiles: [],
        saveNoteNewFolderName: "",
        saveNoteFilename: "",
        newMcpName: "",
        newMcpCommand: "",
        newMcpArgs: "",
        mcpAddError: "",
        expandedServer: null,
        mcpUiOpen: null,
        mcpCallResult: null,
        pickerTarget: null,
        manualThemeModalOpen: false,
        themeRoles: [
            {key: 'accent', label: 'Accent (buttons)'},
            {key: 'accentHover', label: 'Accent hover'},
            {key: 'accentIcon', label: 'Icon accent'},
            {key: 'accentSoft', label: 'Soft highlight'},
            {key: 'accentText', label: 'Accent text'},
            {key: 'accentTextStrong', label: 'Accent text (strong)'},
            {key: 'accentBorder', label: 'Border'},
            {key: 'accentBorderFocus', label: 'Border (focus)'},
            {key: 'wordmark', label: 'Wordmark'},
            {key: 'logoColor', label: 'Logo'},
        ],
        themeDraft: {
            light: {accent:'#7c3aed', accentHover:'#6d28d9', accentIcon:'#8b5cf6', accentSoft:'#f5f3ff', accentText:'#7c3aed', accentTextStrong:'#6d28d9', accentBorder:'#c4b5fd', accentBorderFocus:'#a78bfa', wordmark:'#5eead4', logoColor:'#265152'},
            dark: {accent:'#7c3aed', accentHover:'#8b5cf6', accentIcon:'#8b5cf6', accentSoft:'#8b5cf6', accentText:'#a78bfa', accentTextStrong:'#c4b5fd', accentBorder:'#6d28d9', accentBorderFocus:'#7c3aed', wordmark:'#5eead4', logoColor:'#265152'},
        },
        pickerHue: 258,
        pickerSat: 90,
        pickerVal: 90,
        currentPasswordInput: "",
        newPasswordInput: "",
        passwordChangeMessage: "",
        passwordChangeSuccess: false,
        totpEnabled: false,
        totp2FASetupModalOpen: false,
        totp2FASecret: "",
        totp2FACode: "",
        totp2FAError: "",
        totp2FABackupCodes: [],
        disable2FAModalOpen: false,
        disable2FAPasswordInput: "",
        disable2FAError: "",
        memories: [],
        addMemoryModalOpen: false,
        newMemoryText: "",
        deleteConfirmSessionId: null,
        renameModalOpen: false,
        renameModalSessionId: null,
        renameModalValue: "",
        forkModalOpen: false,
        forkModalMessageIdx: null,
        forkModalValue: "",
        systemStatsOpen: false,
        systemStats: null,
        systemStatsTimer: null,

        // --- notes ---
        notes: [],
        activeNoteId: null,

        async init() {
            await this.loadSettings();
            await this.loadNotes();

            // Load the saved default model, if one was ever set in
            // Settings. This is separate from `model` (the active
            // model for the current chat) so switching models mid-chat
            // never silently overwrites your saved default.
            if (this.defaultModel) {
                this.model = this.defaultModel.value;
                this.modelLabel = this.defaultModel.label;
                this.modelEndpointUrl = this.defaultModel.endpointUrl || "";
            }

            this.loadMemoryModel();
            this.loadAuthStatus();

            // URL hash reflects state as #page/sessionId, e.g.
            // #chat/9f2e... or #settings -- lets refresh/back-button/
            // sharing a link actually restore where you were.
            const hash = window.location.hash.replace(/^#/, "");
            const [hashPage, hashSession] = hash.split("/");
            if (hashPage) this.currentPage = hashPage;
            if (hashSession) {
                this.sessionId = hashSession;
                localStorage.setItem("athena_session", hashSession);
            }

            localStorage.setItem("athena_session", this.sessionId);
            await this.loadSessions();

            this.updateHash();
            this.$watch("currentPage", () => this.updateHash());
            this.$watch("sessionId", () => this.updateHash());

            // Load the actual conversation for whatever session we
            // landed on -- without this, a fresh page load/reload
            // shows a blank chat with no history until the user
            // manually clicks a session in the sidebar.
            this.loadSessionHistory(this.sessionId);
        },

        async loadSessionHistory(id) {
            this.historyReady = false;
            try {
                const resp = await fetch(`/api/history/${id}`);
                const history = await resp.json();
                this.messages = history.map(m => ({
                    ...m,
                    ttsLabel: "Play",
                    rating: null,
                    model: m.role === "assistant" ? (m.model || this.getStoredMsgModel(m.messageId)) : undefined,
                    thinkingOpen: false,
                    toolCalls: m.toolCalls || [],
                    toolsOpen: false,
                }));
                await this.loadRatingsForSession(id);
                await this.loadPinsForSession(id);
                this.forceScrollToBottom();
                // Images referenced in message content load asynchronously
                // AFTER Alpine's own nextTick settles, growing the layout
                // taller a moment later and leaving the scroll position
                // above the true bottom. Re-scroll once any still-loading
                // image actually finishes.
                this.$nextTick(() => {
                    const el = this.$refs.chatBox;
                    if (!el) return;
                    el.querySelectorAll("img").forEach(img => {
                        if (!img.complete) {
                            img.addEventListener("load", () => this.forceScrollToBottom(), {once: true});
                        }
                    });
                });
                // renderMarkdown() does its own Prism syntax highlighting
                // and copy-button wiring inside its own separate nextTick,
                // per message, independent of this one -- for a long
                // history with many code blocks those can still be
                // settling (and still shifting layout height) after the
                // scroll above already ran. A couple of delayed re-scrolls
                // catches that without needing to plumb a real completion
                // signal out of renderMarkdown() itself. historyReady only
                // flips to true after the LAST of these, once the final
                // scroll position should actually be settled -- revealing
                // it any earlier is what showed the top-then-fade-then-
                // snap sequence, since the first scroll attempt is often
                // still short at that point.
                setTimeout(() => this.forceScrollToBottom(), 60);
                setTimeout(() => {
                    this.forceScrollToBottom();
                    this.historyReady = true;
                }, 180);
                this.checkBackgroundGeneration(id);
            } catch (e) {
                console.error("Failed to load session history:", e);
            }
        },
        _startLiveReveal(liveMsg) {
            // A poll only arrives every 2s with however much the real
            // server-side snapshot advanced in that whole window --
            // displaying that instantly reads as chunky "sections
            // spawning in" rather than live streaming. This runs
            // continuously, always chasing whatever the LATEST known
            // target (liveMsg._targetThinking/_targetContent) is, a
            // few characters at a time -- so the display genuinely
            // lags a couple seconds behind the real state, but reads
            // as smooth, continuous generation instead of jumps.
            //
            // If the gap between displayed and target is far bigger
            // than one normal poll window could ever produce (a tab
            // resumed from background, a PWA suspend/resume that kept
            // JS memory alive with a stale position rather than truly
            // reloading), crawling from there at a few chars/tick
            // would take a visibly long time to catch up, and reads
            // as "stuck." Instead, re-anchor once: jump straight to
            // (current - LAG_CHARS), preserving the same intentional
            // lag-behind-live illusion, rather than either sitting on
            // a stale position for a long time or snapping all the
            // way to fully caught-up (which is what caused the
            // earlier chunky "sections spawning in" look).
            const LAG_CHARS = 120;
            const SNAP_THRESHOLD = 400;
            if (liveMsg._revealTimer) return; // already running
            liveMsg._revealTimer = setInterval(() => {
                const targetThinking = liveMsg._targetThinking || "";
                const targetContent = liveMsg._targetContent || "";
                const thinkingGap = targetThinking.length - liveMsg.thinking.length;
                const contentGap = targetContent.length - liveMsg.content.length;
                if (thinkingGap > SNAP_THRESHOLD) {
                    liveMsg.thinking = targetThinking.slice(0, Math.max(liveMsg.thinking.length, targetThinking.length - LAG_CHARS));
                } else if (thinkingGap > 0) {
                    liveMsg.thinking = targetThinking.slice(0, liveMsg.thinking.length + 3);
                } else if (contentGap > SNAP_THRESHOLD) {
                    liveMsg.thinkingOpen = false;
                    liveMsg.content = targetContent.slice(0, Math.max(liveMsg.content.length, targetContent.length - LAG_CHARS));
                } else if (contentGap > 0) {
                    liveMsg.thinkingOpen = false;
                    liveMsg.content = targetContent.slice(0, liveMsg.content.length + 3);
                }
            }, 40);
        },
        _stopLiveReveal(liveMsg) {
            if (liveMsg && liveMsg._revealTimer) {
                clearInterval(liveMsg._revealTimer);
                liveMsg._revealTimer = null;
                // Snap to the true final state immediately once
                // generation is actually done, rather than leaving
                // the last few characters trickling in artificially.
                liveMsg.thinking = liveMsg._targetThinking || liveMsg.thinking;
                liveMsg.content = liveMsg._targetContent || liveMsg.content;
            }
        },
        async checkBackgroundGeneration(sessionId) {
            // A generation can keep running server-side after the tab
            // that started it closed or navigated away -- this checks
            // whether that's happening for the session now being
            // viewed, and shows the actual live snapshot (thinking,
            // content-so-far, tool calls) while it's still going,
            // rather than just a "still generating" placeholder --
            // reusing the exact same message-bubble shape/rendering a
            // real live stream uses, since the status endpoint now
            // returns real snapshot data, not just a boolean. Reloads
            // the real, completed message once it finishes. Re-polls
            // on a timer rather than once, and bails out quietly if
            // the user has since switched to a different session so
            // it doesn't poll forever in the background.
            try {
                const resp = await fetch(`/api/chat/status/${sessionId}`);
                const data = await resp.json();
                if (this.sessionId !== sessionId) return; // navigated away since this check started
                if (data.generating) {
                    this.backgroundGenerating = true;
                    const snap = data.snapshot;
                    if (snap) {
                        let liveMsg = this.messages[this.messages.length - 1];
                        const isFreshReconnect = !liveMsg || !liveMsg._isLiveSnapshot;
                        if (isFreshReconnect) {
                            // Jump straight to wherever the real state already is on a
                            // fresh reload/reconnect -- no reason to "type out" thousands
                            // of already-existing characters from scratch. The gradual
                            // reveal is only for genuinely NEW content arriving between
                            // polls from here on.
                            this.messages.push({role: "assistant", content: snap.content, thinking: snap.thinking, thinkingOpen: !snap.content, toolCalls: snap.tool_calls, toolsOpen: false, model: this.modelLabel, ttsLabel: "Play", rating: null, messageId: null, _isLiveSnapshot: true});
                            liveMsg = this.messages[this.messages.length - 1]; // re-read the actual reactive object, not the pre-push reference
                        }
                        liveMsg._targetThinking = snap.thinking;
                        liveMsg._targetContent = snap.content;
                        liveMsg.toolCalls = snap.tool_calls;
                        this._startLiveReveal(liveMsg);
                    }
                    setTimeout(() => this.checkBackgroundGeneration(sessionId), 2000);
                } else {
                    const wasGenerating = this.backgroundGenerating;
                    this.backgroundGenerating = false;
                    if (wasGenerating) {
                        const liveMsg = this.messages[this.messages.length - 1];
                        if (liveMsg && liveMsg._isLiveSnapshot) this._stopLiveReveal(liveMsg);
                        await this.loadSessionHistory(sessionId);
                    }
                }
            } catch (e) {
                this.backgroundGenerating = false;
            }
        },
        async loadTasks() {
            try {
                const resp = await fetch('/api/tasks');
                this.tasks = await resp.json();
            } catch (e) {
                console.error('Failed to load tasks:', e);
            }
        },
        async loadTaskCapabilities() {
            try {
                const resp = await fetch('/api/tasks/capabilities');
                this.taskCapabilities = await resp.json();
            } catch (e) {
                console.error('Failed to load task capabilities:', e);
            }
        },
        openNewTaskModal() {
            this.editingTaskId = null;
            const defaults = this.taskCapabilities.default_enabled_tools || [];
            this.taskDraft = {
                prompt: '', session_id: null, session_label: 'New task session',
                model: this.model || '', endpoint_url: this.modelEndpointUrl || '', workspace: '',
                schedule_type: 'recurring', run_at: null,
                recurrence: {frequency: 'daily', interval: 1, days_of_week: [], time_of_day: '09:00', end: {type: 'never'}},
                enabled_tools: [...defaults], enabled_skills: [],
            };
            this.taskModalOpen = true;
        },
        openEditTaskModal(task) {
            this.editingTaskId = task.id;
            this.taskDraft = JSON.parse(JSON.stringify(task));
            if (!this.taskDraft.recurrence) {
                this.taskDraft.recurrence = {frequency: 'daily', interval: 1, days_of_week: [], time_of_day: '09:00', end: {type: 'never'}};
            }
            this.taskModalOpen = true;
        },
        async saveTaskDraft() {
            this.taskModalError = '';
            if (!this.taskDraft.prompt.trim()) { this.taskModalError = 'A task needs a prompt.'; return; }
            const payload = {...this.taskDraft};
            if (payload.schedule_type !== 'recurring') payload.recurrence = null;
            try {
                const url = this.editingTaskId ? `/api/tasks/${this.editingTaskId}` : '/api/tasks';
                const method = this.editingTaskId ? 'PUT' : 'POST';
                const resp = await fetch(url, {method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
                const data = await resp.json();
                if (data.error) { this.taskModalError = 'Failed to save: ' + data.error; return; }
                this.taskModalOpen = false;
                await this.loadTasks();
            } catch (e) {
                this.taskModalError = 'Failed to save: ' + e.message;
            }
        },
        deleteTask(id) {
            this.deleteTaskConfirmId = id;
        },
        async confirmDeleteTask() {
            const id = this.deleteTaskConfirmId;
            this.deleteTaskConfirmId = null;
            try {
                await fetch(`/api/tasks/${id}`, {method: 'DELETE'});
                await this.loadTasks();
            } catch (e) {
                console.error('Failed to delete task:', e);
            }
        },
        async toggleTaskStatus(task) {
            const newStatus = task.status === 'paused' ? 'active' : 'paused';
            try {
                await fetch(`/api/tasks/${task.id}/status`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({status: newStatus})});
                await this.loadTasks();
            } catch (e) {
                alert('Failed to update task: ' + e.message);
            }
        },
        toggleTaskTool(name) {
            const arr = this.taskDraft.enabled_tools;
            const idx = arr.indexOf(name);
            if (idx === -1) arr.push(name); else arr.splice(idx, 1);
        },
        toggleTaskSkill(name) {
            const arr = this.taskDraft.enabled_skills;
            const idx = arr.indexOf(name);
            if (idx === -1) arr.push(name); else arr.splice(idx, 1);
        },
        toggleTaskDayOfWeek(day) {
            const arr = this.taskDraft.recurrence.days_of_week;
            const idx = arr.indexOf(day);
            if (idx === -1) arr.push(day); else arr.splice(idx, 1);
        },
        isTaskComplete(task) {
            return task.schedule_type === 'once' && !task.next_run_at && task.last_run_at;
        },
        formatTaskSchedule(task) {
            if (task.schedule_type === 'once') {
                if (!task.next_run_at && task.last_run_at) return 'One-time (completed)';
                return task.run_at ? ('One-time: ' + new Date(task.run_at * 1000).toLocaleString()) : 'One-time';
            }
            const r = task.recurrence;
            if (!r) return 'Recurring';
            const interval = r.interval || 1;
            const t = r.time_of_day || '';
            if (r.frequency === 'weekly') {
                const dayNames = {SU:'Sun',MO:'Mon',TU:'Tue',WE:'Wed',TH:'Thu',FR:'Fri',SA:'Sat'};
                const days = (r.days_of_week || []).map(d => dayNames[d]).join(', ') || 'same day each week';
                return (interval === 1 ? 'Every week' : `Every ${interval} weeks`) + ' on ' + days + ' at ' + t;
            }
            if (r.frequency === 'monthly') {
                return (interval === 1 ? 'Every month' : `Every ${interval} months`) + ' at ' + t;
            }
            return (interval === 1 ? 'Every day' : `Every ${interval} days`) + ' at ' + t;
        },

        getStoredMsgModel(id) {
            if (id === null || id === undefined) return undefined;
            try {
                const map = JSON.parse(localStorage.getItem("athena_msg_models") || "{}");
                return map[id];
            } catch (e) {
                return undefined;
            }
        },
        getModelProviderIcon(modelName) {
            // Maps a model's raw identifier to the Simple Icons slug
            // (simpleicons.org, CDN at cdn.simpleicons.org/:slug) for
            // the company/family that made it -- Qwen gets the Qwen
            // logo, a nemotron model gets the NVIDIA logo, a llama
            // model gets Meta's, etc., matching what Odysseus used to
            // show. Matched by keyword against the model name itself
            // since Ollama tags don't carry structured provider
            // metadata. Order matters where names could overlap (e.g.
            // check specific families before generic ones).
            if (!modelName) return null;
            const name = modelName.toLowerCase();
            const patterns = [
                [/qwen/, 'qwen'],
                [/nemotron|nvidia/, 'nvidia'],
                [/llama|meta-?llama/, 'meta'],
                [/gemma|gemini/, 'google'],
                [/mistral|mixtral|codestral|devstral/, 'mistralai'],
                [/deepseek/, 'deepseek'],
                [/phi-?\d|phi3|phi4/, 'microsoft'],
                [/claude/, 'anthropic'],
                [/gpt-|gpt\d|^o1|^o3|^o4/, 'openai'],
                [/command-?r|cohere/, 'cohere'],
                [/hermes|nous/, 'huggingface'],
            ];
            for (const [re, slug] of patterns) {
                if (re.test(name)) return slug;
            }
            return null;
        },
        setStoredMsgModel(id, model) {
            if (id === null || id === undefined || !model) return;
            try {
                const map = JSON.parse(localStorage.getItem("athena_msg_models") || "{}");
                map[id] = model;
                localStorage.setItem("athena_msg_models", JSON.stringify(map));
            } catch (e) {}
        },

        updateHash() {
            const parts = [this.currentPage];
            if (this.currentPage === "chat") parts.push(this.sessionId);
            window.location.hash = parts.join("/");
        },

        toggleTheme() {
            this.theme = this.theme === "dark" ? "light" : "dark";
            localStorage.setItem("athena_theme", this.theme);
            document.documentElement.style.colorScheme = this.theme;
            if (this.theme === "dark") {
                document.body.classList.add("dark");
            } else {
                document.body.classList.remove("dark");
            }
        },

        // --- session helpers ---
        async loadSessions() {
            try {
                const resp = await fetch("/api/sessions");
                const data = await resp.json();
                if (data.error) {
                    console.error("Failed to load sessions:", data.error);
                    return;
                }
                this.sessions = data.map(s => ({id: s.id, label: s.label, pinned: s.pinned, createdAt: s.created_at, lastActive: s.last_active, parentSessionId: s.parent_session_id}));
            } catch (e) {
                console.error("Failed to load sessions:", e);
            }
        },
        async syncSession(session) {
            session.lastActive = Date.now();
            try {
                await fetch("/api/sessions", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({
                        id: session.id,
                        label: session.label,
                        pinned: !!session.pinned,
                        created_at: session.createdAt,
                        last_active: session.lastActive,
                    }),
                });
            } catch (e) {
                console.error("Failed to sync session:", e);
            }
        },

        sessionSearch: "",

        relativeTime(timestamp) {
            if (!timestamp) return "";
            const diffMs = Date.now() - timestamp;
            const mins = Math.floor(diffMs / 60000);
            if (mins < 1) return "now";
            if (mins < 60) return mins + "m";
            const hours = Math.floor(mins / 60);
            if (hours < 24) return hours + "h";
            const days = Math.floor(hours / 24);
            if (days < 7) return days + "d";
            return new Date(timestamp).toLocaleString(undefined, {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"});
        },

        groupedSessions() {
            const now = new Date();
            const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
            const yesterday = today - 86400000;
            const query = this.sessionSearch.trim().toLowerCase();
            const activityTime = s => s.lastActive || s.createdAt || 0;
            const byRecent = (a, b) => activityTime(b) - activityTime(a);

            // Forks are never shown as their own top-level entry -- they
            // live only under their ancestor's expand arrow. childrenOf
            // maps a session id to its direct forks; collectDescendants
            // flattens the whole subtree (fork-of-a-fork included) into
            // one list under the original ancestor's single arrow,
            // rather than nesting an arrow inside an arrow.
            const childrenOf = new Map();
            for (const s of this.sessions) {
                if (s.parentSessionId) {
                    if (!childrenOf.has(s.parentSessionId)) childrenOf.set(s.parentSessionId, []);
                    childrenOf.get(s.parentSessionId).push(s);
                }
            }
            const collectDescendants = (id, seen) => {
                seen = seen || new Set();
                if (seen.has(id)) return [];
                seen.add(id);
                const direct = childrenOf.get(id) || [];
                let all = direct.slice();
                for (const child of direct) all = all.concat(collectDescendants(child.id, seen));
                return all;
            };

            const topLevel = this.sessions.filter(s => !s.parentSessionId);
            for (const s of topLevel) {
                const descendants = collectDescendants(s.id);
                s.forks = descendants.length ? descendants.sort(byRecent) : undefined;
            }

            const filtered = query
                ? topLevel.filter(s => (s.label || "").toLowerCase().includes(query))
                : topLevel;
            const pinned = filtered.filter(s => s.pinned).sort(byRecent);
            const rest = filtered.filter(s => !s.pinned);
            const groups = {Pinned: pinned, Today: [], Yesterday: [], Earlier: []};
            for (const s of rest) {
                const t = activityTime(s);
                if (t >= today) groups.Today.push(s);
                else if (t >= yesterday) groups.Yesterday.push(s);
                else groups.Earlier.push(s);
            }
            groups.Today.sort(byRecent);
            groups.Yesterday.sort(byRecent);
            groups.Earlier.sort(byRecent);
            return Object.entries(groups)
                .filter(([_, items]) => items.length > 0)
                .map(([label, items]) => ({label, items}));
        },

        toggleForkExpand(id) {
            this.expandedForkSessions[id] = !this.expandedForkSessions[id];
        },

        async loadBots() {
            try {
                const resp = await fetch("/api/bots");
                this.bots = await resp.json();
            } catch (e) {
                console.error("Failed to load bots:", e);
            }
        },
        openNewBotModal() {
            this.botModalEditingId = null;
            this.botDraft = {name: "", model: "", selectedEndpointId: "", description: ""};
            this.botModalOpen = true;
        },
        openEditBotModal(bot) {
            this.botModalEditingId = bot.id;
            const matchingEp = this.endpoints.find(ep => ep.url === bot.endpoint_url && !ep.apiKey);
            this.botDraft = {
                name: bot.name, model: bot.model,
                selectedEndpointId: matchingEp ? matchingEp.id : "",
                description: bot.description || "",
            };
            this.botMenuOpen = null;
            this.botModalOpen = true;
        },
        localBotEndpoints() {
            return this.endpoints.filter(ep => !ep.apiKey);
        },
        botModelOptionsForSelectedEndpoint() {
            const ep = this.endpoints.find(e => e.id === this.currentPickerDraft().selectedEndpointId);
            return ep ? (ep.enabledModels || []) : [];
        },
        onBotEndpointChange() {
            this.botDraft.model = "";
        },
        currentPickerDraft() {
            return this.pickerTarget === "agentDefaults" ? this.agentDefaultsDraft : this.botDraft;
        },
        selectPickerEndpoint(ep) {
            const draft = this.currentPickerDraft();
            draft.selectedEndpointId = ep.id;
            draft.model = "";
            this.botEndpointPopupOpen = false;
        },
        selectPickerModel(m) {
            this.currentPickerDraft().model = m;
            this.botModelPopupOpen = false;
        },
        async openAgentDefaultsModal() {
            try {
                const resp = await fetch("/api/settings");
                const data = await resp.json();
                const defaults = data.bot_creation_defaults || {};
                const matchingEp = this.endpoints.find(ep => ep.url === defaults.endpoint_url && !ep.apiKey);
                this.agentDefaultsDraft = {
                    selectedEndpointId: matchingEp ? matchingEp.id : "",
                    model: defaults.model || "",
                    naming_guidance: defaults.naming_guidance || "",
                };
            } catch (e) {
                this.agentDefaultsDraft = {selectedEndpointId: "", model: "", naming_guidance: ""};
            }
            this.agentDefaultsModalOpen = true;
        },
        async saveAgentDefaults() {
            const ep = this.endpoints.find(e => e.id === this.agentDefaultsDraft.selectedEndpointId);
            const body = JSON.stringify({
                bot_creation_defaults: {
                    endpoint_url: ep ? (ep.url || null) : null,
                    provider: ep ? (ep.provider || "") : "",
                    model: this.agentDefaultsDraft.model || null,
                    naming_guidance: this.agentDefaultsDraft.naming_guidance.trim() || null,
                },
            });
            try {
                await fetch("/api/settings", {method: "POST", headers: {"Content-Type": "application/json"}, body});
                this.agentDefaultsModalOpen = false;
            } catch (e) {
                this.showBotToast("Failed to save agent defaults: " + e.message);
            }
        },
        checkVllmWarning() {
            if (this.botDraft.unload_strategy === "vllm_sleep" && !this.vllmWarnedBotDraft) {
                this.vllmWarnedBotDraft = true;
                this.vllmWarningOpen = true;
            }
        },
        async saveBotDraft() {
            if (!this.botDraft.name.trim() || !this.botDraft.selectedEndpointId || !this.botDraft.model) {
                this.showBotToast("Name, endpoint, and model are required.");
                return;
            }
            const ep = this.endpoints.find(e => e.id === this.botDraft.selectedEndpointId);
            const body = JSON.stringify({
                name: this.botDraft.name.trim(),
                model: this.botDraft.model,
                endpoint_url: ep ? (ep.url || null) : null,
                provider: ep ? (ep.provider || "") : "",
                unload_strategy: "ollama_keep_alive",
                description: this.botDraft.description.trim() || null,
                allowed_tools: [],
            });
            try {
                const url = this.botModalEditingId ? `/api/bots/${this.botModalEditingId}` : "/api/bots";
                const method = this.botModalEditingId ? "PUT" : "POST";
                const resp = await fetch(url, {method, headers: {"Content-Type": "application/json"}, body});
                const data = await resp.json();
                if (data.error) {
                    this.showBotToast("Failed to save bot: " + data.error);
                    return;
                }
                this.botModalOpen = false;
                await this.loadBots();
            } catch (e) {
                this.showBotToast("Failed to save bot: " + e.message);
            }
        },
        async deleteBot(id) {
            this.botMenuOpen = null;
            try {
                await fetch(`/api/bots/${id}`, {method: "DELETE"});
                if (this.activeBotId === id) {
                    this.activeBotId = null;
                    this.activeRoomId = null;
                    this.roomMessages = [];
                }
                await this.loadBots();
            } catch (e) {
                this.showBotToast("Failed to delete bot: " + e.message);
            }
        },
        async openBotDM(botId) {
            this.activeBotId = botId;
            this.activeAthenaAgent = false;
            this.botConversationLoading = true;
            try {
                const resp = await fetch("/api/rooms/find_or_create", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({kind: "dm", human_party: "user", member_bot_ids: [botId], label: "DM"}),
                });
                const room = await resp.json();
                if (room.error) {
                    this.showBotToast("Failed to open conversation: " + room.error);
                    return;
                }
                this.activeRoom = room;
                this.activeRoomId = room.id;
                await this.loadRoomMessages(room.id);
            } catch (e) {
                this.showBotToast("Failed to open conversation: " + e.message);
            } finally {
                this.botConversationLoading = false;
            }
        },
        async openGroupRoom(room) {
            this.activeBotId = null;
            this.activeAthenaAgent = false;
            this.activeRoom = room;
            this.activeRoomId = room.id;
            this.botConversationLoading = true;
            try {
                await this.loadRoomMessages(room.id);
            } finally {
                this.botConversationLoading = false;
            }
        },
        async loadGroupRooms() {
            try {
                const resp = await fetch("/api/rooms");
                const data = await resp.json();
                this.groupRooms = (data || []).filter(r => r.kind === "group" && !r.is_everyone_room);
            } catch (e) {
                this.groupRooms = [];
            }
        },
        async openEveryoneRoom() {
            this.activeBotId = null;
            this.activeAthenaAgent = false;
            this.botConversationLoading = true;
            try {
                const resp = await fetch("/api/rooms/everyone", {method: "POST"});
                const room = await resp.json();
                if (room.error) {
                    this.showBotToast("Failed to open group: " + room.error);
                    return;
                }
                this.activeRoom = room;
                this.activeRoomId = room.id;
                await this.loadRoomMessages(room.id);
            } catch (e) {
                this.showBotToast("Failed to open group: " + e.message);
            } finally {
                this.botConversationLoading = false;
            }
        },
        openNewGroupModal() {
            this.groupDraft = {label: "", memberBotIds: []};
            this.groupModalOpen = true;
        },
        toggleGroupMember(botId) {
            const idx = this.groupDraft.memberBotIds.indexOf(botId);
            if (idx === -1) this.groupDraft.memberBotIds.push(botId);
            else this.groupDraft.memberBotIds.splice(idx, 1);
        },
        mentionSuggestions() {
            const q = this.mentionQuery.toLowerCase();
            const botMatches = this.bots.filter(b => b.name.toLowerCase().startsWith(q));
            const results = [];
            if ("athena".startsWith(q)) {
                results.push({id: "athena", name: "Athena", isAthena: true});
            }
            return results.concat(botMatches).slice(0, 6);
        },
        onComposerInput(event) {
            const text = event.target.value;
            const cursorPos = event.target.selectionStart;
            const textBeforeCursor = text.slice(0, cursorPos);
            const atMatch = textBeforeCursor.match(/@([A-Za-z0-9_-]*)$/);
            if (atMatch) {
                this.mentionQuery = atMatch[1];
                this.mentionStartIndex = cursorPos - atMatch[0].length;
                this.mentionPopupOpen = true;
                this.mentionActiveIndex = 0;
            } else {
                this.mentionPopupOpen = false;
            }
        },
        onComposerKeydown(event) {
            if (this.mentionPopupOpen) {
                const suggestions = this.mentionSuggestions();
                if (event.key === "ArrowDown") {
                    event.preventDefault();
                    this.mentionActiveIndex = Math.min(this.mentionActiveIndex + 1, Math.max(suggestions.length - 1, 0));
                    return;
                }
                if (event.key === "ArrowUp") {
                    event.preventDefault();
                    this.mentionActiveIndex = Math.max(this.mentionActiveIndex - 1, 0);
                    return;
                }
                if ((event.key === "Enter" || event.key === "Tab") && suggestions.length > 0) {
                    event.preventDefault();
                    this.selectMention(suggestions[this.mentionActiveIndex]);
                    return;
                }
                if (event.key === "Escape") {
                    event.preventDefault();
                    this.mentionPopupOpen = false;
                    return;
                }
            }
            if (event.key === "Enter") {
                event.preventDefault();
                this.sendBotComposerMessage();
            }
        },
        selectMention(bot) {
            const before = this.botComposerText.slice(0, this.mentionStartIndex);
            const after = this.botComposerText.slice(this.mentionStartIndex + 1 + this.mentionQuery.length);
            this.botComposerText = before + "@" + bot.name + " " + after;
            this.mentionPopupOpen = false;
            this.$nextTick(() => {
                const ta = this.$refs.botComposerTextarea;
                if (ta) {
                    ta.focus();
                    const newPos = before.length + bot.name.length + 2;
                    ta.setSelectionRange(newPos, newPos);
                }
            });
        },
        async createGroupRoom() {
            if (!this.groupDraft.label.trim()) {
                this.showBotToast("Give the group a name.");
                return;
            }
            if (this.groupDraft.memberBotIds.length < 2) {
                this.showBotToast("Pick at least two bots for a group.");
                return;
            }
            try {
                const resp = await fetch("/api/rooms/find_or_create", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({kind: "group", human_party: null, member_bot_ids: this.groupDraft.memberBotIds, label: this.groupDraft.label.trim()}),
                });
                const room = await resp.json();
                if (room.error) {
                    this.showBotToast("Failed to create group: " + room.error);
                    return;
                }
                this.groupModalOpen = false;
                await this.loadGroupRooms();
                await this.openGroupRoom(room);
            } catch (e) {
                this.showBotToast("Failed to create group: " + e.message);
            }
        },
        async openAthenaAgent() {
            this.activeBotId = null;
            this.activeAthenaAgent = true;
            this.activeRoomId = null;
            if (!this.athenaAgentModel) {
                try {
                    const settingsResp = await fetch("/api/settings");
                    const settingsData = await settingsResp.json();
                    const saved = settingsData.athena_agent_model;
                    if (saved && saved.model) {
                        this.athenaAgentModel = saved.model;
                        this.athenaAgentModelLabel = saved.label || saved.model;
                        this.athenaAgentModelEndpointUrl = saved.endpoint_url || "";
                        this.athenaAgentModelProvider = saved.provider || "";
                        this.athenaAgentModelApiKey = saved.api_key || "";
                    }
                } catch (e) {
                    console.error("Failed to load Athena's saved model:", e);
                }
            }
            this.botConversationLoading = true;
            try {
                const resp = await fetch(`/api/history/athena-bots-agent`);
                const data = await resp.json();
                this.roomMessages = data.map(m => ({
                    sender_type: m.role === "assistant" ? "athena" : "user",
                    content: m.content,
                    messageId: m.messageId,
                    thinking: m.thinking || "",
                    thinkingOpen: false,
                    toolCalls: m.toolCalls || [],
                    toolsOpen: false,
                }));
            } catch (e) {
                this.roomMessages = [];
            } finally {
                this.botConversationLoading = false;
            }
            this.checkAthenaAgentBackgroundGeneration();
        },
        async checkAthenaAgentBackgroundGeneration() {
            // Same mechanism as main chat's checkBackgroundGeneration --
            // Athena2 goes through the identical /api/chat pipeline, just
            // under the fixed "athena-bots-agent" session id, so a
            // generation she started can keep running after the tab
            // closed and this picks it back up the same way. Now also
            // renders the real live snapshot (thinking, content-so-far,
            // tool calls) instead of just a "still generating" spinner.
            try {
                const resp = await fetch(`/api/chat/status/athena-bots-agent`);
                const data = await resp.json();
                if (!this.activeAthenaAgent) return; // navigated away since this check started
                if (data.generating) {
                    this.botConversationLoading = true;
                    const snap = data.snapshot;
                    if (snap) {
                        let liveMsg = this.roomMessages[this.roomMessages.length - 1];
                        const isFreshReconnect = !liveMsg || !liveMsg._isLiveSnapshot;
                        if (isFreshReconnect) {
                            this.roomMessages.push({sender_type: "athena", content: snap.content, thinking: snap.thinking, thinkingOpen: !snap.content, toolCalls: snap.tool_calls, toolsOpen: false, _isLiveSnapshot: true});
                            liveMsg = this.roomMessages[this.roomMessages.length - 1]; // re-read the actual reactive object, not the pre-push reference
                        }
                        liveMsg._targetThinking = snap.thinking;
                        liveMsg._targetContent = snap.content;
                        liveMsg.toolCalls = snap.tool_calls;
                        this._startLiveReveal(liveMsg);
                    }
                    setTimeout(() => this.checkAthenaAgentBackgroundGeneration(), 2000);
                } else {
                    const wasGenerating = this.botConversationLoading;
                    this.botConversationLoading = false;
                    if (wasGenerating) {
                        const liveMsg = this.roomMessages[this.roomMessages.length - 1];
                        if (liveMsg && liveMsg._isLiveSnapshot) this._stopLiveReveal(liveMsg);
                        await this.openAthenaAgent();
                    }
                }
            } catch (e) {
                this.botConversationLoading = false;
            }
        },
        showBotToast(msg) {
            this.botToastMessage = msg;
            clearTimeout(this.botToastTimer);
            this.botToastTimer = setTimeout(() => { this.botToastMessage = ""; }, 4000);
        },
        closeBotConversation() {
            this.activeAthenaAgent = false;
            this.activeRoomId = null;
            this.activeBotId = null;
            this.roomMessages = [];
        },
        async loadRoomMessages(roomId) {
            try {
                const resp = await fetch(`/api/rooms/${roomId}/messages`);
                this.roomMessages = await resp.json();
            } catch (e) {
                this.roomMessages = [];
            }
        },
        async sendBotComposerMessage() {
            const text = this.botComposerText.trim();
            if (!text) return;
            this.botComposerText = "";
            if (this.activeAthenaAgent) {
                this.roomMessages.push({sender_type: "user", content: text});
                this.botConversationLoading = true;
                try {
                    const resp = await fetch("/api/chat", {
                        method: "POST",
                        headers: {"Content-Type": "application/json"},
                        body: JSON.stringify({session_id: "athena-bots-agent", message: text, model: this.athenaAgentModel || this.model, endpoint_url: this.athenaAgentModelEndpointUrl || "", provider: this.athenaAgentModelProvider || ""}),
                    });
                    const reader = resp.body.getReader();
                    const decoder = new TextDecoder();
                    let full = "";
                    const msgIndex = this.roomMessages.length;
                    this.roomMessages.push({sender_type: "athena", content: "", thinking: "", thinkingOpen: true, toolCalls: []});
                    while (true) {
                        const {done, value} = await reader.read();
                        if (done) break;
                        for (const line of decoder.decode(value).split("\n")) {
                            if (!line.startsWith("data: ")) continue;
                            try {
                                const obj = JSON.parse(line.slice(6));
                                if (obj.thinking) {
                                    this.roomMessages[msgIndex].thinking += obj.thinking;
                                }
                                if (obj.delta) {
                                    this.roomMessages[msgIndex].thinkingOpen = false;
                                    full += obj.delta;
                                    this.roomMessages[msgIndex].content = full;
                                }
                                if (obj.type === "tool_start") {
                                    this.roomMessages[msgIndex].toolCalls.push({tool: obj.tool, status: "running", output: null, open: true});
                                }
                                if (obj.type === "tool_output") {
                                    const tc = this.roomMessages[msgIndex].toolCalls.find(t => t.tool === obj.tool && t.status === "running");
                                    if (tc) {
                                        tc.status = "done";
                                        tc.output = obj.output;
                                        tc.open = false;
                                    }
                                }
                            } catch (e) {}
                        }
                    }
                } catch (e) {
                    this.showBotToast("Failed to message Athena: " + e.message);
                } finally {
                    this.botConversationLoading = false;
                }
            } else if (this.activeRoomId) {
                this.roomMessages.push({sender_type: "user", content: text});
                this.botConversationLoading = true;
                try {
                    const resp = await fetch(`/api/rooms/${this.activeRoomId}/send`, {
                        method: "POST",
                        headers: {"Content-Type": "application/json"},
                        body: JSON.stringify({sender_type: "user", content: text}),
                    });
                    const data = await resp.json();
                    if (data.error) {
                        this.showBotToast("Failed to send: " + data.error);
                        return;
                    }
                    await this.loadRoomMessages(this.activeRoomId);
                } catch (e) {
                    this.showBotToast("Failed to send: " + e.message);
                } finally {
                    this.botConversationLoading = false;
                }
            }
        },

        newChat() {
            this.sessionId = generateUUID();
            localStorage.setItem("athena_session", this.sessionId);
            this.messages = [];
        },

        async switchSession(id) {
            // Hide the chat container before anything else changes --
            // otherwise messages = [] renders below at full visibility
            // for a moment before loadSessionHistory's own historyReady
            // flip even runs, flashing the empty-state welcome screen
            // in between the old chat and the new one.
            this.historyReady = false;
            this.backgroundGenerating = false;
            this.sessionId = id;
            localStorage.setItem("athena_session", id);
            this.messages = [];
            this.sessionMenuOpen = null;
            await this.loadSessionHistory(id);
        },

        togglePin(id) {
            const s = this.sessions.find(x => x.id === id);
            if (s) {
                s.pinned = !s.pinned;
                this.syncSession(s);
            }
            this.sessionMenuOpen = null;
        },

        renameSession(id) {
            const s = this.sessions.find(x => x.id === id);
            if (!s) return;
            this.renameModalSessionId = id;
            this.renameModalValue = s.label;
            this.renameModalOpen = true;
            this.sessionMenuOpen = null;
        },

        confirmRename() {
            const s = this.sessions.find(x => x.id === this.renameModalSessionId);
            if (s && this.renameModalValue.trim()) {
                s.label = this.renameModalValue.trim();
                this.syncSession(s);
            }
            this.renameModalOpen = false;
        },

        deleteSession(id) {
            this.deleteConfirmSessionId = id;
            this.sessionMenuOpen = null;
        },

        async deleteMessagePair(msg) {
            if (!msg.messageId) return;
            try {
                const resp = await fetch(`/api/messages/${msg.messageId}`, {method: "DELETE"});
                const data = await resp.json();
                if (data.error) {
                    alert("Failed to delete message: " + data.error);
                    return;
                }
                const deletedIds = new Set(data.deleted_ids || [msg.messageId]);
                this.messages = this.messages.filter(m => !m.messageId || !deletedIds.has(m.messageId));
            } catch (e) {
                alert("Failed to delete message: " + e.message);
            }
        },
        async deleteRoomMessagePair(msg) {
            if (!msg.messageId) return;
            try {
                const resp = await fetch(`/api/messages/${msg.messageId}`, {method: "DELETE"});
                const data = await resp.json();
                if (data.error) {
                    alert("Failed to delete message: " + data.error);
                    return;
                }
                const deletedIds = new Set(data.deleted_ids || [msg.messageId]);
                this.roomMessages = this.roomMessages.filter(m => !m.messageId || !deletedIds.has(m.messageId));
            } catch (e) {
                alert("Failed to delete message: " + e.message);
            }
        },
        forkFromMessage(idx) {
            const msg = this.messages[idx];
            if (!msg || !msg.messageId) return;
            this.forkModalMessageIdx = idx;
            const current = this.sessions.find(s => s.id === this.sessionId);
            this.forkModalValue = "Fork of " + (current ? current.label : "chat");
            this.forkModalOpen = true;
        },
        async confirmFork() {
            const idx = this.forkModalMessageIdx;
            const msg = this.messages[idx];
            this.forkModalOpen = false;
            if (!msg || !msg.messageId) return;
            const label = this.forkModalValue.trim() || "Forked chat";
            try {
                const resp = await fetch(`/api/sessions/${this.sessionId}/fork`, {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({message_id: msg.messageId, label: label}),
                });
                const data = await resp.json();
                if (data.error) {
                    alert("Failed to fork: " + data.error);
                    return;
                }
                await this.loadSessions();
                await this.switchSession(data.session_id);
            } catch (e) {
                alert("Failed to fork: " + e.message);
            }
        },
        async confirmDelete() {
            const id = this.deleteConfirmSessionId;
            this.deleteConfirmSessionId = null;
            let deletedIds = [id];
            try {
                const resp = await fetch(`/api/history/${id}`, {method: "DELETE"});
                const data = await resp.json();
                if (Array.isArray(data.sessions_deleted)) deletedIds = data.sessions_deleted;
            } catch (e) {
                console.error("Failed to delete session from LCM:", e);
            }
            const deletedSet = new Set(deletedIds);
            this.sessions = this.sessions.filter(x => !deletedSet.has(x.id));
            if (deletedSet.has(this.sessionId)) this.newChat();
        },
        toggleSystemStats() {
            this.systemStatsOpen = !this.systemStatsOpen;
            if (this.systemStatsOpen) {
                this.fetchSystemStats();
                this.systemStatsTimer = setInterval(() => this.fetchSystemStats(), 2000);
            } else if (this.systemStatsTimer) {
                clearInterval(this.systemStatsTimer);
                this.systemStatsTimer = null;
            }
        },
        async fetchSystemStats() {
            try {
                const resp = await fetch("/api/system/stats");
                this.systemStats = await resp.json();
            } catch (e) {
                console.error("Failed to fetch system stats:", e);
            }
        },

        selectModel(m) {
            if (this.modelPopupTarget === "athena") {
                this.athenaAgentModel = m.value;
                this.athenaAgentModelLabel = m.label;
                this.athenaAgentModelEndpointUrl = m.endpointUrl || "";
                this.athenaAgentModelProvider = m.provider || "";
                this.athenaAgentModelApiKey = m.apiKey || "";
                fetch("/api/settings", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
                    athena_agent_model: {model: m.value, label: m.label, endpoint_url: m.endpointUrl || "", provider: m.provider || "", api_key: m.apiKey || ""}
                })}).catch(e => console.error("Failed to save Athena's model:", e));
            } else {
                this.model = m.value;
                this.modelLabel = m.label;
                this.modelEndpointUrl = m.endpointUrl || "";
                this.modelProvider = m.provider || "";
                this.modelApiKey = m.apiKey || "";
            }
            this.modelPopupOpen = false;
        },

        saveSearchUrl() {
            fetch("/api/settings", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({search_url: this.searchUrl})}).catch(e => console.error("Failed to save search URL:", e));
        },

        saveDelegationConcurrency() {
            const n = Math.max(1, parseInt(this.delegationConcurrency, 10) || 1);
            this.delegationConcurrency = n;
            fetch("/api/settings", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({delegation_concurrency: n})}).catch(e => console.error("Failed to save delegation concurrency:", e));
        },

        saveBotPtcEnabled() {
            fetch("/api/settings", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({bot_ptc_enabled: this.botPtcEnabled})}).catch(e => console.error("Failed to save PTC setting:", e));
        },

        saveAsyncDelegationEnabled() {
            fetch("/api/settings", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({delegation_async_enabled: this.asyncDelegationEnabled})}).catch(e => console.error("Failed to save async delegation setting:", e));
        },

        saveLayaGatingEnabled() {
            fetch("/api/settings", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({laya_gating_enabled: this.layaGatingEnabled})}).catch(e => console.error("Failed to save Laya gating setting:", e));
        },

        openSaveNoteFilePopup() {
            this.saveNoteFilePopupOpen = true;
            this.saveNoteNewFolderName = "";
            this.saveNoteFilename = (this.activeNote().title || "note") + ".md";
            this.loadSaveNoteBrowse("");
        },
        async loadSaveNoteBrowse(path) {
            try {
                const resp = await fetch(`/api/workspace/browse?path=${encodeURIComponent(path)}`);
                const data = await resp.json();
                if (data.error) {
                    alert("Browse error: " + data.error);
                    return;
                }
                this.saveNoteBrowsePath = data.path;
                this.saveNoteBrowseParent = data.parent;
                this.saveNoteBrowseDirs = data.directories;
                this.saveNoteBrowseFiles = data.files || [];
            } catch (e) {
                alert("Browse failed: " + e.message);
            }
        },
        saveNoteBrowseUp() {
            if (this.saveNoteBrowseParent) {
                this.loadSaveNoteBrowse(this.saveNoteBrowseParent);
            }
        },
        saveNoteBrowseInto(dirName) {
            const next = this.saveNoteBrowsePath.replace(/\/$/, "") + "/" + dirName;
            this.loadSaveNoteBrowse(next);
        },
        async createSaveNoteFolder() {
            if (!this.saveNoteNewFolderName.trim()) return;
            try {
                const resp = await fetch("/api/workspace/mkdir", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({path: this.saveNoteBrowsePath, name: this.saveNoteNewFolderName.trim()}),
                });
                const data = await resp.json();
                if (data.error) {
                    alert("Create folder error: " + data.error);
                    return;
                }
                this.saveNoteNewFolderName = "";
                this.loadSaveNoteBrowse(this.saveNoteBrowsePath);
            } catch (e) {
                alert("Create folder failed: " + e.message);
            }
        },
        async confirmSaveNoteToFile() {
            if (!this.saveNoteFilename.trim()) {
                alert("Enter a filename.");
                return;
            }
            try {
                const resp = await fetch("/api/notes/save-to-file", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({
                        path: this.saveNoteBrowsePath,
                        filename: this.saveNoteFilename.trim(),
                        content: this.activeNote().body || "",
                    }),
                });
                const data = await resp.json();
                if (data.error) {
                    alert("Save error: " + data.error);
                    return;
                }
                this.saveNoteFilePopupOpen = false;
            } catch (e) {
                alert("Save failed: " + e.message);
            }
        },
        openWorkspacePopup() {
            this.workspacePopupOpen = true;
            this.newFolderName = "";
            this.loadWorkspaceBrowse(this.workspace || "");
        },

        async loadWorkspaceBrowse(path) {
            try {
                const resp = await fetch(`/api/workspace/browse?path=${encodeURIComponent(path)}`);
                const data = await resp.json();
                if (data.error) {
                    alert("Browse error: " + data.error);
                    return;
                }
                this.workspaceBrowsePath = data.path;
                this.workspaceBrowseParent = data.parent;
                this.workspaceBrowseDirs = data.directories;
            } catch (e) {
                alert("Browse failed: " + e.message);
            }
        },

        workspaceBrowseUp() {
            if (this.workspaceBrowseParent) {
                this.loadWorkspaceBrowse(this.workspaceBrowseParent);
            }
        },

        workspaceBrowseInto(dirName) {
            const next = this.workspaceBrowsePath.replace(/\/$/, "") + "/" + dirName;
            this.loadWorkspaceBrowse(next);
        },

        async createWorkspaceFolder() {
            if (!this.newFolderName.trim()) return;
            try {
                const resp = await fetch("/api/workspace/mkdir", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({path: this.workspaceBrowsePath, name: this.newFolderName.trim()}),
                });
                const data = await resp.json();
                if (data.error) {
                    alert("Create folder error: " + data.error);
                    return;
                }
                this.newFolderName = "";
                this.loadWorkspaceBrowse(this.workspaceBrowsePath);
            } catch (e) {
                alert("Create folder failed: " + e.message);
            }
        },

        setAsDefaultModel() {
            if (!this.model) return;
            this.defaultModel = {value: this.model, label: this.modelLabel, endpointUrl: this.modelEndpointUrl};
            fetch("/api/settings", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({default_model: this.defaultModel})}).catch(e => console.error("Failed to save default model:", e));
        },

        async loadMemories() {
            try {
                const resp = await fetch("/api/memory");
                const data = await resp.json();
                if (data.error) {
                    console.error("Failed to load memories:", data.error);
                    return;
                }
                this.memories = data;
            } catch (e) {
                console.error("Failed to load memories:", e);
            }
        },
        async addMemory() {
            const text = this.newMemoryText.trim();
            if (!text) return;
            try {
                const resp = await fetch("/api/memory", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({content: text}),
                });
                const data = await resp.json();
                if (data.error) {
                    alert("Failed to add memory: " + data.error);
                    return;
                }
                this.addMemoryModalOpen = false;
                await this.loadMemories();
            } catch (e) {
                alert("Failed to add memory: " + e.message);
            }
        },
        async deleteMemory(id) {
            try {
                await fetch(`/api/memory/${id}`, {method: "DELETE"});
                this.memories = this.memories.filter(m => m.id !== id);
            } catch (e) {
                console.error("Failed to delete memory:", e);
            }
        },
        async loadAuthStatus() {
            try {
                const resp = await fetch("/api/auth/status");
                const data = await resp.json();
                this.totpEnabled = !!data.totp_enabled;
            } catch (e) {
                console.error("Failed to load auth status:", e);
            }
        },
        async changePassword() {
            this.passwordChangeMessage = "";
            try {
                const resp = await fetch("/api/auth/change-password", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({current_password: this.currentPasswordInput, new_password: this.newPasswordInput}),
                });
                const data = await resp.json();
                if (data.error) {
                    this.passwordChangeSuccess = false;
                    this.passwordChangeMessage = data.error;
                    return;
                }
                this.passwordChangeSuccess = true;
                this.passwordChangeMessage = "Password updated.";
                this.currentPasswordInput = "";
                this.newPasswordInput = "";
            } catch (e) {
                this.passwordChangeSuccess = false;
                this.passwordChangeMessage = "Failed: " + e.message;
            }
        },
        async start2FASetup() {
            this.totp2FACode = "";
            this.totp2FAError = "";
            this.totp2FABackupCodes = [];
            try {
                const resp = await fetch("/api/auth/2fa/setup", {method: "POST"});
                const data = await resp.json();
                if (data.error) {
                    alert("Failed to start 2FA setup: " + data.error);
                    return;
                }
                this.totp2FASecret = data.secret;
                this.totp2FASetupModalOpen = true;
                this.$nextTick(() => {
                    const el = document.getElementById("totp-qr-canvas");
                    if (window.QRCode && el) {
                        el.innerHTML = "";
                        new QRCode(el, {text: data.otpauth_uri, width: 256, height: 256});
                    }
                });
            } catch (e) {
                alert("Failed to start 2FA setup: " + e.message);
            }
        },
        async confirm2FA() {
            this.totp2FAError = "";
            try {
                const resp = await fetch("/api/auth/2fa/confirm", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({code: this.totp2FACode}),
                });
                const data = await resp.json();
                if (data.error) {
                    this.totp2FAError = data.error;
                    return;
                }
                this.totpEnabled = true;
                this.totp2FABackupCodes = data.backup_codes;
            } catch (e) {
                this.totp2FAError = "Failed: " + e.message;
            }
        },
        async disable2FA() {
            this.disable2FAError = "";
            try {
                const resp = await fetch("/api/auth/2fa/disable", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({password: this.disable2FAPasswordInput}),
                });
                const data = await resp.json();
                if (data.error) {
                    this.disable2FAError = data.error;
                    return;
                }
                this.totpEnabled = false;
                this.disable2FAModalOpen = false;
            } catch (e) {
                this.disable2FAError = "Failed: " + e.message;
            }
        },
        async logout() {
            try {
                await fetch("/api/auth/logout", {method: "POST"});
            } catch (e) {}
            window.location.href = "/login";
        },
        async loadSettings() {
            try {
                const resp = await fetch("/api/settings");
                const data = await resp.json();
                if (data.workspace !== undefined) this.workspace = data.workspace;
                if (data.endpoints !== undefined) this.endpoints = data.endpoints;
                if (data.model_aliases !== undefined) this.modelAliases = data.model_aliases;
                if (data.search_url !== undefined) this.searchUrl = data.search_url;
                if (data.delegation_concurrency !== undefined && data.delegation_concurrency !== null) this.delegationConcurrency = data.delegation_concurrency;
                if (data.bot_ptc_enabled !== undefined && data.bot_ptc_enabled !== null) this.botPtcEnabled = data.bot_ptc_enabled;
                if (data.delegation_async_enabled !== undefined && data.delegation_async_enabled !== null) this.asyncDelegationEnabled = data.delegation_async_enabled;
                if (data.laya_gating_enabled !== undefined && data.laya_gating_enabled !== null) this.layaGatingEnabled = data.laya_gating_enabled;
                if (data.default_model !== undefined) this.defaultModel = data.default_model;
                if (data.theme) {
                    this.customTheme = data.theme;
                    this.applyCustomTheme(data.theme);
                    if (data.theme.seedColor) this.themeSeedColor = data.theme.seedColor;
                }
            } catch (e) {
                console.error("Failed to load settings:", e);
            }
        },
        _hexToRgbaFaded(hex, alpha) {
            hex = hex.replace('#', '');
            const r = parseInt(hex.substring(0,2), 16);
            const g = parseInt(hex.substring(2,4), 16);
            const b = parseInt(hex.substring(4,6), 16);
            return `rgba(${r}, ${g}, ${b}, ${alpha})`;
        },
        openManualThemeModal() {
            const LOGO_BASE_HUE = 181;
            const t = this.customTheme;
            const reconstructLogo = (hueDeg) => this._hslToHex(LOGO_BASE_HUE + (hueDeg || 0), 37, 24);
            const reconstructSoft = (val) => {
                if (!val) return '#8b5cf6';
                if (val.startsWith('#')) return val;
                // Dark mode's accentSoft is saved as rgba(...) (see
                // _hexToRgbaFaded at apply time), not hsla(...) -- this
                // branch was missing entirely, so reopening the editor
                // always fell through to the hardcoded default below,
                // silently discarding whatever the user had actually
                // picked for dark mode specifically.
                const rgbMatch = val.match(/rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/);
                if (rgbMatch) {
                    const toHex = (n) => parseInt(n, 10).toString(16).padStart(2, '0');
                    return `#${toHex(rgbMatch[1])}${toHex(rgbMatch[2])}${toHex(rgbMatch[3])}`;
                }
                const m = val.match(/hsla?\(([\d.]+)/);
                if (m) return this._hslToHex(parseFloat(m[1]), 70, 60);
                return '#8b5cf6';
            };
            if (t) {
                this.themeDraft = {
                    light: {
                        accent: t.light.accent, accentHover: t.light.accentHover, accentIcon: t.light.accentIcon,
                        accentSoft: reconstructSoft(t.light.accentSoft), accentText: t.light.accentText,
                        accentTextStrong: t.light.accentTextStrong, accentBorder: t.light.accentBorder,
                        accentBorderFocus: t.light.accentBorderFocus, wordmark: t.light.wordmark,
                        // Read the actual saved logoColor directly, same
                        // as every other field here -- reconstructLogo(hue)
                        // was the old hue-rotation-era approximation and
                        // is now stale/wrong, only kept as a fallback for
                        // themes saved before logoColor existed at all.
                        logoColor: t.light.logoColor || reconstructLogo(t.light.logoHue),
                    },
                    dark: {
                        accent: t.dark.accent || t.light.accent, accentHover: t.dark.accentHover || t.light.accentHover,
                        accentIcon: t.dark.accentIcon || t.light.accentIcon, accentSoft: reconstructSoft(t.dark.accentSoft),
                        accentText: t.dark.accentText, accentTextStrong: t.dark.accentTextStrong,
                        accentBorder: t.dark.accentBorder, accentBorderFocus: t.dark.accentBorderFocus,
                        wordmark: t.dark.wordmark, logoColor: t.dark.logoColor || reconstructLogo(t.dark.logoHue),
                    },
                };
            }
            this.manualThemeModalOpen = true;
        },
        applyManualTheme() {
            const LOGO_BASE_HUE = 181;
            const buildMode = (draft) => {
                const logoHsl = this._hexToHsl(draft.logoColor);
                const logoHue = Math.round(((logoHsl.h - LOGO_BASE_HUE) % 360 + 360) % 360);
                return {
                    accent: draft.accent, accentHover: draft.accentHover, accentIcon: draft.accentIcon,
                    accentText: draft.accentText, accentTextStrong: draft.accentTextStrong,
                    accentBorder: draft.accentBorder, accentBorderFocus: draft.accentBorderFocus,
                    wordmark: draft.wordmark, logoHue: logoHue, logoColor: draft.logoColor,
                    accentSoft: draft.accentSoft,
                };
            };
            const light = buildMode(this.themeDraft.light);
            const dark = buildMode(this.themeDraft.dark);
            dark.accentSoft = this._hexToRgbaFaded(this.themeDraft.dark.accentSoft, 0.1);
            this.saveTheme({light, dark, seedColor: this.themeSeedColor});
            this.manualThemeModalOpen = false;
        },
        _getPickerColor() {
            if (!this.pickerTarget) return this.themeSeedColor;
            return this.themeDraft[this.pickerTarget.scope][this.pickerTarget.key];
        },
        _setPickerColor(hex) {
            if (!this.pickerTarget) {
                this.themeSeedColor = hex;
            } else {
                this.themeDraft[this.pickerTarget.scope][this.pickerTarget.key] = hex;
            }
        },
        _sniffResultShape(parsed) {
            // Reacts to STRUCTURE only, never to which tool or server
            // produced it -- an array of objects with an image-looking
            // field and a title-looking field renders as a card grid,
            // whether that's HighlightHunter's VODs or some unrelated
            // future tool's file list.
            if (!parsed || typeof parsed !== "object") return null;
            let arr = Array.isArray(parsed) ? parsed : null;
            if (!arr) {
                for (const key of Object.keys(parsed)) {
                    if (Array.isArray(parsed[key])) { arr = parsed[key]; break; }
                }
            }
            if (!arr || arr.length === 0 || typeof arr[0] !== "object" || arr[0] === null) return null;

            const keys = Object.keys(arr[0]);
            const imageKey = keys.find(k => /thumbnail|image|thumb|avatar|icon/i.test(k) && typeof arr[0][k] === "string");
            const titleKey = keys.find(k => /title|name|label/i.test(k) && typeof arr[0][k] === "string");

            if (imageKey && titleKey) {
                return {type: "cards", items: arr, imageKey, titleKey};
            }
            return null;
        },
        async callMcpTool(server, tool) {
            this.mcpCallResult = {loading: true};
            try {
                const resp = await fetch("/api/mcp/call", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({server: server, tool: tool, arguments: {}}),
                });
                const data = await resp.json();
                this.mcpCallResult = {
                    raw: data.raw_text || JSON.stringify(data, null, 2),
                    shape: this._sniffResultShape(data.parsed),
                    isError: data.isError,
                };
            } catch (e) {
                this.mcpCallResult = {raw: "Request failed: " + e.message};
            }
        },
        uploadSkillFile(event) {
            const file = event.target.files[0];
            if (!file) return;
            this.skillUploadError = "";
            const reader = new FileReader();
            reader.onload = async () => {
                try {
                    const resp = await fetch("/api/skills/parse", {
                        method: "POST",
                        headers: {"Content-Type": "application/json"},
                        body: JSON.stringify({content: reader.result}),
                    });
                    const data = await resp.json();
                    if (data.error) {
                        this.skillUploadError = data.error;
                        return;
                    }
                    this.skillDraft = {name: data.name, description: data.description, body: data.body, folder: null};
                    this.skillFormOpen = true;
                } catch (e) {
                    this.skillUploadError = "Failed to parse file: " + e.message;
                }
            };
            reader.readAsText(file);
            event.target.value = "";
        },
        async loadSkills() {
            try {
                const resp = await fetch("/api/skills");
                const data = await resp.json();
                this.skills = data.skills || [];
            } catch (e) {
                console.error("Failed to load skills:", e);
            }
        },
        openSkillForm(skill) {
            if (skill) {
                fetch("/api/skills/" + encodeURIComponent(skill.folder))
                    .then(r => r.json())
                    .then(data => {
                        this.skillDraft = {name: data.name, description: data.description, body: data.body, folder: data.folder};
                        this.skillFormOpen = true;
                    });
            } else {
                this.skillDraft = {name: "", description: "", body: "", folder: null};
                this.skillFormOpen = true;
            }
        },
        async saveSkill() {
            if (!this.skillDraft.name || !this.skillDraft.description) {
                alert("Name and description are required.");
                return;
            }
            try {
                await fetch("/api/skills", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify(this.skillDraft),
                });
                this.skillFormOpen = false;
                await this.loadSkills();
            } catch (e) {
                alert("Failed to save skill: " + e.message);
            }
        },
        async deleteSkill(folder) {
            if (!confirm("Delete this skill?")) return;
            try {
                await fetch("/api/skills/" + encodeURIComponent(folder), {method: "DELETE"});
                await this.loadSkills();
            } catch (e) {
                console.error("Failed to delete skill:", e);
            }
        },
        async loadMcpServers() {
            try {
                const resp = await fetch("/api/mcp/servers");
                const data = await resp.json();
                this.mcpServers = data.servers || [];
            } catch (e) {
                console.error("Failed to load MCP servers:", e);
            }
        },
        async addMcpServer() {
            this.mcpAddError = "";
            if (!this.newMcpName || !this.newMcpCommand) {
                this.mcpAddError = "Name and command are required.";
                return;
            }
            try {
                const resp = await fetch("/api/mcp/servers", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({
                        name: this.newMcpName,
                        command: this.newMcpCommand,
                        args: this.newMcpArgs.trim() ? this.newMcpArgs.trim().split(/\s+/) : [],
                    }),
                });
                const data = await resp.json();
                if (!data.connected) {
                    this.mcpAddError = data.error || "Failed to connect.";
                } else {
                    this.newMcpName = "";
                    this.newMcpCommand = "";
                    this.newMcpArgs = "";
                }
                await this.loadMcpServers();
            } catch (e) {
                this.mcpAddError = "Request failed: " + e.message;
            }
        },
        async removeMcpServer(name) {
            try {
                await fetch("/api/mcp/servers/" + encodeURIComponent(name), {method: "DELETE"});
                await this.loadMcpServers();
            } catch (e) {
                console.error("Failed to remove MCP server:", e);
            }
        },
        openColorPicker() {
            this.pickerTarget = null;
            const hsv = this._hexToHsv(this._getPickerColor());
            this.pickerHue = hsv.h;
            this.pickerSat = hsv.s;
            this.pickerVal = hsv.v;
            this.colorPickerOpen = true;
        },
        openManualPicker(scope, key) {
            this.pickerTarget = {scope, key};
            const hsv = this._hexToHsv(this._getPickerColor());
            this.pickerHue = hsv.h;
            this.pickerSat = hsv.s;
            this.pickerVal = hsv.v;
            this.colorPickerOpen = true;
        },
        syncPickerFromHex() {
            const hsv = this._hexToHsv(this._getPickerColor());
            this.pickerHue = hsv.h;
            this.pickerSat = hsv.s;
            this.pickerVal = hsv.v;
        },
        _updateFromPicker() {
            this._setPickerColor(this._hsvToHex(this.pickerHue, this.pickerSat, this.pickerVal));
        },
        _dragSL(e, rect) {
            const clientX = e.touches ? e.touches[0].clientX : e.clientX;
            const clientY = e.touches ? e.touches[0].clientY : e.clientY;
            let x = (clientX - rect.left) / rect.width;
            let y = (clientY - rect.top) / rect.height;
            x = Math.max(0, Math.min(1, x));
            y = Math.max(0, Math.min(1, y));
            this.pickerSat = x * 100;
            this.pickerVal = (1 - y) * 100;
            this._updateFromPicker();
        },
        startDragSL(e) {
            e.preventDefault();
            const rect = e.currentTarget.getBoundingClientRect();
            this._dragSL(e, rect);
            const move = (ev) => this._dragSL(ev, rect);
            const up = () => {
                document.removeEventListener('mousemove', move);
                document.removeEventListener('mouseup', up);
                document.removeEventListener('touchmove', move);
                document.removeEventListener('touchend', up);
            };
            document.addEventListener('mousemove', move);
            document.addEventListener('mouseup', up);
            document.addEventListener('touchmove', move, {passive: false});
            document.addEventListener('touchend', up);
        },
        _dragHue(e, rect) {
            const clientX = e.touches ? e.touches[0].clientX : e.clientX;
            let x = (clientX - rect.left) / rect.width;
            x = Math.max(0, Math.min(1, x));
            this.pickerHue = x * 360;
            this._updateFromPicker();
        },
        startDragHue(e) {
            e.preventDefault();
            const rect = e.currentTarget.getBoundingClientRect();
            this._dragHue(e, rect);
            const move = (ev) => this._dragHue(ev, rect);
            const up = () => {
                document.removeEventListener('mousemove', move);
                document.removeEventListener('mouseup', up);
                document.removeEventListener('touchmove', move);
                document.removeEventListener('touchend', up);
            };
            document.addEventListener('mousemove', move);
            document.addEventListener('mouseup', up);
            document.addEventListener('touchmove', move, {passive: false});
            document.addEventListener('touchend', up);
        },
        _hexToHsv(hex) {
            hex = hex.replace('#', '');
            const r = parseInt(hex.substring(0,2), 16) / 255;
            const g = parseInt(hex.substring(2,4), 16) / 255;
            const b = parseInt(hex.substring(4,6), 16) / 255;
            const max = Math.max(r,g,b), min = Math.min(r,g,b);
            const d = max - min;
            let h = 0;
            if (d !== 0) {
                switch (max) {
                    case r: h = ((g-b)/d) % 6; break;
                    case g: h = (b-r)/d + 2; break;
                    default: h = (r-g)/d + 4; break;
                }
                h *= 60;
                if (h < 0) h += 360;
            }
            const v = max;
            const s = max === 0 ? 0 : d / max;
            return {h, s: s*100, v: v*100};
        },
        _hsvToHex(h, s, v) {
            s = Math.max(0, Math.min(100, s)) / 100;
            v = Math.max(0, Math.min(100, v)) / 100;
            h = ((h % 360) + 360) % 360;
            const c = v * s;
            const x = c * (1 - Math.abs((h/60) % 2 - 1));
            const m = v - c;
            let r, g, b;
            if (h < 60) { r=c; g=x; b=0; }
            else if (h < 120) { r=x; g=c; b=0; }
            else if (h < 180) { r=0; g=c; b=x; }
            else if (h < 240) { r=0; g=x; b=c; }
            else if (h < 300) { r=x; g=0; b=c; }
            else { r=c; g=0; b=x; }
            const toHex = v => Math.round((v+m)*255).toString(16).padStart(2, '0');
            return '#' + toHex(r) + toHex(g) + toHex(b);
        },
        _hexToHsl(hex) {
            hex = hex.replace('#', '');
            const r = parseInt(hex.substring(0,2), 16) / 255;
            const g = parseInt(hex.substring(2,4), 16) / 255;
            const b = parseInt(hex.substring(4,6), 16) / 255;
            const max = Math.max(r,g,b), min = Math.min(r,g,b);
            let h, s, l = (max+min)/2;
            if (max === min) { h = s = 0; }
            else {
                const d = max - min;
                s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
                switch (max) {
                    case r: h = (g-b)/d + (g < b ? 6 : 0); break;
                    case g: h = (b-r)/d + 2; break;
                    default: h = (r-g)/d + 4; break;
                }
                h /= 6;
            }
            return {h: h*360, s: s*100, l: l*100};
        },
        _hslToHex(h, s, l) {
            h = ((h % 360) + 360) % 360;
            s = Math.max(0, Math.min(100, s)) / 100;
            l = Math.max(0, Math.min(100, l)) / 100;
            const c = (1 - Math.abs(2*l - 1)) * s;
            const x = c * (1 - Math.abs((h/60) % 2 - 1));
            const m = l - c/2;
            let r, g, b;
            if (h < 60) { r=c; g=x; b=0; }
            else if (h < 120) { r=x; g=c; b=0; }
            else if (h < 180) { r=0; g=c; b=x; }
            else if (h < 240) { r=0; g=x; b=c; }
            else if (h < 300) { r=x; g=0; b=c; }
            else { r=c; g=0; b=x; }
            const toHex = v => Math.round((v+m)*255).toString(16).padStart(2, '0');
            return '#' + toHex(r) + toHex(g) + toHex(b);
        },
        generateThemeFromColor(seedHex) {
            // Base hue of the ORIGINAL logo teal (rgb 38,81,82), needed
            // because hue-rotate() shifts by a DELTA, not an absolute
            // value -- so the logo's rotation must be computed relative
            // to its own real starting hue, not the picked color alone.
            const LOGO_BASE_HUE = 181;
            const hsl = this._hexToHsl(seedHex);
            const h = hsl.h;
            const s = Math.min(hsl.s, 90);
            const logoHue = Math.round(((h - LOGO_BASE_HUE) % 360 + 360) % 360);
            const light = {
                accent: this._hslToHex(h, s, 50),
                accentHover: this._hslToHex(h, s, 42),
                accentIcon: this._hslToHex(h, s, 60),
                accentSoft: this._hslToHex(h, Math.min(s, 60), 96),
                accentText: this._hslToHex(h, s, 50),
                accentTextStrong: this._hslToHex(h, s, 42),
                accentBorder: this._hslToHex(h, Math.min(s, 65), 80),
                accentBorderFocus: this._hslToHex(h, s, 70),
                wordmark: this._hslToHex(h, Math.min(s + 10, 80), 55),
                logoHue: logoHue,
                logoColor: this._hslToHex(h, Math.min(s, 50), 28),
            };
            const dark = {
                accentSoft: `hsla(${h.toFixed(0)}, ${s.toFixed(0)}%, 60%, 0.1)`,
                accentText: this._hslToHex(h, s, 70),
                accentTextStrong: this._hslToHex(h, s, 80),
                accentBorder: this._hslToHex(h, s, 42),
                accentBorderFocus: this._hslToHex(h, s, 50),
                wordmark: this._hslToHex(h, Math.min(s + 10, 80), 65),
                logoHue: logoHue,
                logoColor: this._hslToHex(h, Math.min(s, 50), 28),
            };
            return {light, dark, seedColor: seedHex};
        },
        applyCustomTheme(theme) {
            const CSS_VAR_MAP = {
                accent: '--accent', accentHover: '--accent-hover', accentIcon: '--accent-icon',
                accentSoft: '--accent-soft', accentText: '--accent-text', accentTextStrong: '--accent-text-strong',
                accentBorder: '--accent-border', accentBorderFocus: '--accent-border-focus',
                wordmark: '--wordmark-color',
            };
            let styleEl = document.getElementById('custom-theme-vars');
            if (!styleEl) {
                styleEl = document.createElement('style');
                styleEl.id = 'custom-theme-vars';
                document.head.appendChild(styleEl);
            }
            const buildBlock = (colors) => {
                if (!colors) return '';
                return Object.entries(colors).map(([key, val]) => {
                    if (key === 'logoHue') return `--athena-logo-hue: ${val}deg;`;
                    if (key === 'logoColor') {
                        const hsl = this._hexToHsl(val);
                        const shade2 = this._hslToHex(hsl.h, hsl.s, hsl.l + 8.6);
                        const shade3 = this._hslToHex(hsl.h, hsl.s, hsl.l + 12.9);
                        const shade4 = this._hslToHex(hsl.h, hsl.s, hsl.l + 2.4);
                        return `--athena-logo-color: ${val}; --athena-logo-shade2: ${shade2}; --athena-logo-shade3: ${shade3}; --athena-logo-shade4: ${shade4};`;
                    }
                    const varName = CSS_VAR_MAP[key];
                    return varName ? `${varName}: ${val};` : '';
                }).join(' ');
            };
            styleEl.textContent = `:root { ${buildBlock(theme.light)} } .dark { ${buildBlock(theme.dark)} }`;
        },
        async saveTheme(theme) {
            this.customTheme = theme;
            this.applyCustomTheme(theme);
            try {
                await fetch("/api/settings", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({theme: theme}),
                });
            } catch (e) {
                console.error("Failed to save theme:", e);
            }
        },
        async loadMemoryModel() {
            try {
                const resp = await fetch("/api/settings/memory-model");
                const data = await resp.json();
                this.memoryExtractionModel = data.model || "";
            } catch (e) {
                console.error("Failed to load memory model setting:", e);
            }
        },
        async saveMemoryModel() {
            try {
                await fetch("/api/settings/memory-model", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({model: this.memoryExtractionModel || null}),
                });
            } catch (e) {
                console.error("Failed to save memory model setting:", e);
            }
        },
        allAvailableModels() {
            const list = [];
            for (const ep of this.endpoints) {
                for (const m of (ep.enabledModels || [])) {
                    const displayName = this.modelAliases[m] || m;
                    const entry = {value: m, label: `${displayName} (${ep.name})`, endpointUrl: ep.url};
                    if (ep.type === "online") {
                        entry.provider = ep.provider;
                        entry.apiKey = ep.apiKey;
                    }
                    list.push(entry);
                }
            }
            return list;
        },
        startRenameModel(m, event) {
            event.stopPropagation();
            this.renamingModelValue = m.value;
            this.renameInputText = this.modelAliases[m.value] || m.value;
        },
        saveModelRename() {
            const trimmed = this.renameInputText.trim();
            if (trimmed && trimmed !== this.renamingModelValue) {
                this.modelAliases[this.renamingModelValue] = trimmed;
            } else {
                delete this.modelAliases[this.renamingModelValue];
            }
            fetch("/api/settings", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({model_aliases: this.modelAliases})}).catch(e => console.error("Failed to save model alias:", e));
            this.renamingModelValue = null;
        },
        cancelRenameModel() {
            this.renamingModelValue = null;
        },

        saveEndpoints() {
            fetch("/api/settings", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({endpoints: this.endpoints})}).catch(e => console.error("Failed to save endpoints:", e));
        },

        addEndpoint() {
            const ep = {
                id: generateUUID(),
                name: this.newEndpointName || (this.newEndpointType === "local" ? "Local Endpoint" : "Online Provider"),
                type: this.newEndpointType,
                url: this.newEndpointUrl,
                provider: this.newEndpointType === "local" ? "" : this.newEndpointProvider,
                apiKey: this.newEndpointType === "local" ? "" : this.newEndpointApiKey,
                modelNames: this.newEndpointModelNames || "",
                detectedModels: [],
                enabledModels: [],
                detecting: false,
                error: "",
            };
            this.endpoints.push(ep);
            this.saveEndpoints();
            this.addEndpointOpen = false;
            this.newEndpointName = "";
            this.newEndpointUrl = "";
            this.newEndpointApiKey = "";
            this.newEndpointModelNames = "";
        },

        deleteEndpoint(id) {
            if (!confirm("Remove this endpoint?")) return;
            this.endpoints = this.endpoints.filter(e => e.id !== id);
            this.saveEndpoints();
        },

        async detectModels(id) {
            const ep = this.endpoints.find(e => e.id === id);
            if (!ep) return;
            ep.detecting = true;
            ep.error = "";
            try {
                const resp = await fetch("/api/detect-models", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({url: ep.url, type: ep.type || "", provider: ep.provider || "", api_key: ep.apiKey || ""}),
                });
                const data = await resp.json();
                if (data.error) {
                    ep.error = data.error;
                } else {
                    ep.detectedModels = data.models || [];
                    const prevEnabled = new Set(ep.enabledModels || []);
                    const prevKnown = new Set(ep.detectedModels);
                    ep.enabledModels = ep.detectedModels.filter(m => !prevKnown.has(m) || prevEnabled.has(m));
                    if (ep.enabledModels.length === 0) ep.enabledModels = [...ep.detectedModels];
                }
            } catch (e) {
                ep.error = "Request failed: " + e.message;
            } finally {
                ep.detecting = false;
                this.saveEndpoints();
            }
        },
        filteredEndpointModels(ep) {
            const list = ep.detectedModels || [];
            const q = (ep.modelFilter || "").trim().toLowerCase();
            if (!q) return list;
            return list.filter(m => m.toLowerCase().includes(q));
        },
        selectAllFilteredModels(ep) {
            // Operates on whatever's currently visible (respects an
            // active search filter) rather than the endpoint's full
            // detected list, so "search free, then select all" only
            // selects the free ones, without touching any other
            // already-enabled models outside that filter.
            const visible = this.filteredEndpointModels(ep);
            const enabledSet = new Set(ep.enabledModels || []);
            for (const m of visible) enabledSet.add(m);
            ep.enabledModels = [...enabledSet];
            this.saveEndpoints();
        },
        deselectAllFilteredModels(ep) {
            const visibleSet = new Set(this.filteredEndpointModels(ep));
            ep.enabledModels = (ep.enabledModels || []).filter(m => !visibleSet.has(m));
            this.saveEndpoints();
        },

        toggleModelEnabled(endpointId, modelName) {
            const ep = this.endpoints.find(e => e.id === endpointId);
            if (!ep) return;
            const idx = ep.enabledModels.indexOf(modelName);
            if (idx === -1) {
                ep.enabledModels.push(modelName);
            } else {
                ep.enabledModels.splice(idx, 1);
            }
            this.saveEndpoints();
        },

        syncCtxFromText() {
            const val = parseInt((this.maxCtxText || "0").replace(/[^0-9]/g, ""), 10) || 0;
            this.maxCtx = Math.min(val, 131072);
            this.maxCtxText = String(val);
        },

        renderMarkdown(text) {
            if (!text) return "";
            let html;
            try {
                html = marked.parse(text);
            } catch (e) {
                return text;
            }
            this.$nextTick(() => {
                if (window.Prism) Prism.highlightAll();
                document.querySelectorAll(".prose-msg pre:not(.copy-wired)").forEach(pre => {
                    pre.classList.add("copy-wired");
                    const btn = document.createElement("button");
                    btn.className = "copy-btn";
                    btn.textContent = "Copy";
                    btn.onclick = () => {
                        navigator.clipboard.writeText(pre.innerText);
                        btn.textContent = "Copied!";
                        setTimeout(() => btn.textContent = "Copy", 1500);
                    };
                    const wrapper = document.createElement("div");
        wrapper.className = "pre-copy-wrap";
        wrapper.style.position = "relative";
        pre.parentElement.replaceChild(wrapper, pre);
        wrapper.appendChild(pre);
        btn.style.position = "absolute";
        btn.style.top = "8px";
        btn.style.right = "8px";
        wrapper.appendChild(btn);
                });
                document.querySelectorAll(".prose-msg table:not(.scroll-wired)").forEach(table => {
                    table.classList.add("scroll-wired");
                    const wrap = document.createElement("div");
                    wrap.className = "table-scroll-wrap";
                    table.parentElement.replaceChild(wrap, table);
                    wrap.appendChild(table);
                    const updateFade = () => {
                        const scrollable = wrap.scrollWidth > wrap.clientWidth + 1;
                        wrap.classList.toggle("fade-left", scrollable && wrap.scrollLeft > 4);
                        wrap.classList.toggle("fade-right", scrollable && wrap.scrollLeft < wrap.scrollWidth - wrap.clientWidth - 4);
                    };
                    wrap.addEventListener("scroll", updateFade);
                    window.addEventListener("resize", updateFade);
                    updateFade();
                });
            });
            return html;
        },

        autoGrow(e) {
            e.target.style.height = "auto";
            e.target.style.height = e.target.scrollHeight + "px";
        },
        handleComposerEnter(e) {
            // Matches the app's own md: breakpoint (768px) used
            // everywhere else for mobile vs desktop layout, rather than
            // touch-detection, which is unreliable on hybrid devices.
            const isMobile = window.innerWidth < 768;
            if (isMobile || e.shiftKey) {
                return; // let Enter (or Shift+Enter on desktop) insert a newline naturally
            }
            e.preventDefault();
            this.send();
        },

        _scrollThinkingToBottom() {
            this.$nextTick(() => {
                const boxes = document.querySelectorAll('.thinking-scroll-target');
                if (boxes.length) {
                    const last = boxes[boxes.length - 1];
                    // Same guard as scrollToBottom() -- unconditionally
                    // forcing this on every thinking chunk made it
                    // impossible to scroll up and read earlier thinking
                    // content, since it snapped back down on the very
                    // next chunk.
                    const distanceFromBottom = last.scrollHeight - last.scrollTop - last.clientHeight;
                    if (distanceFromBottom < 100) {
                        last.scrollTop = last.scrollHeight;
                    }
                }
            });
        },
        forceScrollToBottom() {
            this.$nextTick(() => {
                const el = this.$refs.chatBox;
                if (el) el.scrollTop = el.scrollHeight;
            });
        },
        scrollToBottom() {
            this.$nextTick(() => {
                const el = this.$refs.chatBox;
                if (!el) return;
                // Only auto-follow if already near the bottom -- otherwise
                // this fights a deliberate scroll-up during generation,
                // snapping the view back down before the user can read
                // anything.
                const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
                if (distanceFromBottom < 150) {
                    el.scrollTop = el.scrollHeight;
                }
            });
        },

        async send() {
            const text = this.inputText.trim();
            if ((!text && !this.pendingImages.length && !this.pendingAttachments.length) || this.sending) return;
            this.inputText = "";
            this.$nextTick(() => {
                if (this.$refs.textareaEl) this.$refs.textareaEl.style.height = "auto";
            });
            const imagesToSend = this.pendingImages.map(img => img.base64);
            const attachmentsToSend = this.pendingAttachments.map(att => ({name: att.name, content: att.content}));
            this.pendingImages = [];
            this.pendingAttachments = [];
            this.sending = true;

            this.messages.push({role: "user", content: text, hasImage: imagesToSend.length > 0, created_at: Date.now()});
            let activeSession = this.sessions.find(s => s.id === this.sessionId);
            if (!activeSession) {
                activeSession = {id: this.sessionId, label: text.slice(0, 40), createdAt: Date.now(), pinned: false};
                this.sessions.unshift(activeSession);
                this.syncSession(activeSession);
            } else if (activeSession.label === "New chat") {
                activeSession.label = text.slice(0, 40);
                this.syncSession(activeSession);
            } else {
                this.syncSession(activeSession);
            }
            this.forceScrollToBottom();

            const assistantMsg = {role: "assistant", content: "", thinking: "", thinkingOpen: true, ctxUsed: null, promptTokens: null, tokensPerSec: null, model: this.modelLabel, ttsLabel: "Play", toolCalls: [], toolsOpen: false, rating: null, messageId: null, created_at: Date.now()};
            this.messages.push(assistantMsg);
            const msgIndex = this.messages.length - 1;

            this.currentAbortController = new AbortController();
            try {
                const resp = await fetch("/api/chat", {
                    method: "POST",
                    signal: this.currentAbortController.signal,
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({
                        session_id: this.sessionId,
                        message: text,
                        model: this.model,
                        max_ctx: this.maxCtx,
                        workspace: this.workspace,
                        endpoint_url: this.modelEndpointUrl,
                        search_url: this.searchUrl,
                        images: imagesToSend,
                        attachments: attachmentsToSend,
                        provider: this.modelProvider,
                        api_key: this.modelApiKey,
                    }),
                });

                const reader = resp.body.getReader();
                const decoder = new TextDecoder();
                let buffer = "";

                while (true) {
                    const {done, value} = await reader.read();
                    if (done) break;
                    buffer += decoder.decode(value, {stream: true});
                    const lines = buffer.split("\n\n");
                    buffer = lines.pop();
                    for (const line of lines) {
                        if (!line.startsWith("data: ")) continue;
                        const data = JSON.parse(line.slice(6));
                        if (data.user_message_id !== undefined) {
                            if (this.messages[msgIndex - 1]) {
                                this.messages[msgIndex - 1].messageId = data.user_message_id;
                            }
                        }
                        if (data.thinking) {
                            this.messages[msgIndex].thinking += data.thinking;
                            this.scrollToBottom();
                            this._scrollThinkingToBottom();
                        }
                        if (data.delta) {
                            this.messages[msgIndex].thinkingOpen = false;
                            this._streamBuffer += data.delta;
                            if (!this._streamFlushTimer) {
                                this._streamFlushTimer = setTimeout(() => {
                                    this.messages[msgIndex].content += this._streamBuffer;
                                    this._streamBuffer = "";
                                    this._streamFlushTimer = null;
                                    this.scrollToBottom();
                                }, 80);
                            }
                        }
                        if (data.type === "tool_start") {
                            this.messages[msgIndex].toolCalls.push({tool: data.tool, status: "running", output: null, open: true});
                            this.scrollToBottom();
                        }
                        if (data.type === "tool_output") {
                            const tc = this.messages[msgIndex].toolCalls.find(t => t.tool === data.tool && t.status === "running");
                            if (tc) {
                                tc.status = "done";
                                tc.output = data.output;
                                tc.open = false;
                            }
                            this.scrollToBottom();
                        }
                        if (data.done) {
                            if (this._streamFlushTimer) {
                                clearTimeout(this._streamFlushTimer);
                                this._streamFlushTimer = null;
                            }
                            if (this._streamBuffer) {
                                this.messages[msgIndex].content += this._streamBuffer;
                                this._streamBuffer = "";
                            }
                            this.messages[msgIndex].ctxUsed = data.ctx_used;
                            this.messages[msgIndex].promptTokens = data.prompt_tokens;
                            this.messages[msgIndex].tokensPerSec = data.tokens_per_sec;
                        }
                        if (data.assistant_message_id !== undefined) {
                            this.messages[msgIndex].messageId = data.assistant_message_id;
                            this.setStoredMsgModel(data.assistant_message_id, this.messages[msgIndex].model);
                        }
                    }
                }
            } catch (e) {
                if (e.name === "AbortError") {
                    this.messages[msgIndex].content += (this.messages[msgIndex].content ? "\n\n" : "") + "_Stopped._";
                } else {
                    this.messages[msgIndex].content = "⚠️ Error: " + e.message;
                }
            } finally {
                this.sending = false;
                this.currentAbortController = null;
            }
        },
        stopGeneration() {
            fetch("/api/chat/cancel", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({session_id: this.sessionId}),
            }).catch(() => {});
            if (this.currentAbortController) {
                this.currentAbortController.abort();
            }
        },


        selectWorkspace(path) {
            this.workspace = path;
            fetch("/api/settings", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({workspace: path})}).catch(e => console.error("Failed to save workspace:", e));
            this.workspacePopupOpen = false;
            if (this.fileBrowserOpen) {
                this.loadFileBrowser("");
            }
        },
        toggleFileBrowser() {
            if (!this.workspace) return;
            this.fileBrowserOpen = !this.fileBrowserOpen;
            if (this.fileBrowserOpen) {
                this.loadFileBrowser("");
            }
        },
        async loadFileBrowser(path) {
            try {
                const resp = await fetch(`/api/workspace/files?workspace=${encodeURIComponent(this.workspace)}&path=${encodeURIComponent(path)}`);
                const data = await resp.json();
                if (data.error) {
                    alert("File browser error: " + data.error);
                    return;
                }
                this.fileBrowserPath = data.path;
                if (!this.fileBrowserPath) {
                    this.fileBrowserParent = null;
                } else {
                    const parts = this.fileBrowserPath.split("/");
                    parts.pop();
                    this.fileBrowserParent = parts.join("/");
                }
                this.fileBrowserEntries = data.entries;
            } catch (e) {
                alert("File browser failed: " + e.message);
            }
        },
        fileBrowserUp() {
            if (this.fileBrowserParent !== null) {
                this.loadFileBrowser(this.fileBrowserParent);
            }
        },
        fileBrowserInto(name) {
            const next = this.fileBrowserPath ? this.fileBrowserPath + "/" + name : name;
            this.loadFileBrowser(next);
        },
        async deleteFileBrowserEntry(name, type) {
            const relPath = this.fileBrowserPath ? this.fileBrowserPath + "/" + name : name;
            const label = type === "dir" ? "folder (and everything inside it)" : "file";
            if (!confirm(`Delete this ${label}: "${name}"?`)) return;
            try {
                const resp = await fetch(`/api/workspace/delete?workspace=${encodeURIComponent(this.workspace)}&path=${encodeURIComponent(relPath)}`, {
                    method: "DELETE",
                });
                const data = await resp.json();
                if (data.error) {
                    alert("Delete error: " + data.error);
                    return;
                }
                this.loadFileBrowser(this.fileBrowserPath);
            } catch (e) {
                alert("Delete failed: " + e.message);
            }
        },
        async previewFile(name) {
            const relPath = this.fileBrowserPath ? this.fileBrowserPath + "/" + name : name;
            try {
                const resp = await fetch(`/api/workspace/read?workspace=${encodeURIComponent(this.workspace)}&path=${encodeURIComponent(relPath)}`);
                const data = await resp.json();
                if (data.error) {
                    alert("Preview error: " + data.error);
                    return;
                }
                this.filePreviewPath = data.path;
                this.filePreviewContent = data.content;
                this.filePreviewOpen = true;
            } catch (e) {
                alert("Preview failed: " + e.message);
            }
        },
        formatFileSize(bytes) {
            if (bytes === null || bytes === undefined) return "";
            if (bytes < 1024) return bytes + " B";
            if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
            return (bytes / (1024 * 1024)).toFixed(1) + " MB";
        },
        handleImageSelected(event) {
            const files = Array.from(event.target.files || []);
            files.forEach(file => {
                const reader = new FileReader();
                reader.onload = () => {
                    const dataUrl = reader.result;
                    const base64 = dataUrl.split(",")[1];
                    this.pendingImages.push({name: file.name, base64: base64, previewUrl: dataUrl});
                };
                reader.readAsDataURL(file);
            });
            event.target.value = "";
        },
        handleFileSelected(event) {
            const files = Array.from(event.target.files || []);
            files.forEach(file => {
                const reader = new FileReader();
                reader.onload = () => {
                    this.pendingAttachments.push({name: file.name, content: reader.result});
                };
                reader.readAsText(file);
            });
            event.target.value = "";
        },
        removeImage(idx) {
            this.pendingImages.splice(idx, 1);
        },
        removeAttachment(idx) {
            this.pendingAttachments.splice(idx, 1);
        },

        copyMessage(text, event) {
            navigator.clipboard.writeText(text);
            const btn = event.currentTarget;
            const original = btn.innerHTML;
            btn.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
            setTimeout(() => btn.innerHTML = original, 1200);
        },

        async retryMessage(msgIndex) {
            // Find the user message that preceded this assistant response,
            // remove everything from there onward, and resend it -- same
            // effect as if you'd typed it again.
            let userIdx = msgIndex - 1;
            while (userIdx >= 0 && this.messages[userIdx].role !== "user") userIdx--;
            if (userIdx < 0) return;
            const text = this.messages[userIdx].content;
            this.messages = this.messages.slice(0, userIdx);
            this.inputText = text;
            await this.send();
        },

        async rateMessage(msgIndex, rating) {
            const msg = this.messages[msgIndex];
            if (!msg.messageId) {
                alert("Can't rate this message yet -- it hasn't finished saving.");
                return;
            }

            // Clicking the already-selected rating removes it entirely.
            const newRating = msg.rating === rating ? "" : rating;
            let reason = "";
            if (newRating === "down") {
                reason = prompt("What was wrong with this response? (optional)") || "";
            }

            const previousRating = msg.rating;
            msg.rating = newRating || null; // update immediately, revert on failure

            try {
                const resp = await fetch("/api/rate", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({
                        message_id: msg.messageId,
                        session_id: this.sessionId,
                        model: this.model,
                        rating: newRating,
                        reason: reason,
                    }),
                });
                if (!resp.ok) throw new Error("Request failed");
            } catch (e) {
                msg.rating = previousRating; // real failure -- don't show a rating that didn't actually save
                alert("Couldn't save rating: " + e.message);
            }
        },

        async loadRatingsForSession(sessionId) {
            // Re-applies which rating button should show as selected
            // after a reload -- ratings live server-side, not in the
            // message content LCM returns, so this has to run as a
            // separate fetch whenever a session's history loads.
            try {
                const resp = await fetch(`/api/ratings/${sessionId}`);
                const ratings = await resp.json();
                for (const msg of this.messages) {
                    if (msg.messageId && ratings[msg.messageId]) {
                        msg.rating = ratings[msg.messageId].rating || null;
                    }
                }
            } catch (e) {
                // Non-critical -- ratings just won't show as pre-selected.
            }
        },

        async pinMessage(msgIndex, event) {
            if (event) event.stopPropagation();
            const msg = this.messages[msgIndex];
            if (!msg.messageId) {
                alert("Can't pin this message yet -- it hasn't finished saving.");
                return;
            }
            const newPinned = !msg.pinned;
            const previousPinned = msg.pinned;
            msg.pinned = newPinned; // update immediately, revert on failure
            try {
                const resp = await fetch("/api/pin", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({
                        message_id: msg.messageId,
                        session_id: this.sessionId,
                        content: msg.content,
                        pinned: newPinned,
                    }),
                });
                if (!resp.ok) throw new Error("Request failed");
            } catch (e) {
                msg.pinned = previousPinned; // real failure -- don't show a pin that didn't actually save
                alert("Couldn't save pin: " + e.message);
            }
        },
        async loadPinsForSession(sessionId) {
            // Re-applies which messages show the pin toggle as active
            // after a reload -- pins live server-side (a content
            // snapshot, not a reference), separate fetch same as ratings.
            try {
                const resp = await fetch(`/api/pins/${sessionId}`);
                const pins = await resp.json();
                for (const msg of this.messages) {
                    if (msg.messageId && pins[msg.messageId]) {
                        msg.pinned = true;
                    }
                }
            } catch (e) {
                // Non-critical -- pins just won't show as pre-selected.
            }
        },
        pinnedMessages() {
            return this.messages
                .map((msg, idx) => ({msg, idx}))
                .filter(m => m.msg.pinned);
        },
        scrollToPinnedMessage(idx) {
            this.pinnedMenuOpen = false;
            this.$nextTick(() => {
                const el = document.getElementById('msg-' + idx);
                if (!el) return;
                el.scrollIntoView({behavior: 'smooth', block: 'center'});
                el.classList.add('pinned-jump-highlight');
                setTimeout(() => el.classList.remove('pinned-jump-highlight'), 1200);
            });
        },

        async playTTS(text, event) {
            const msg = this.messages.find(m => m.content === text);
            if (msg) msg.ttsLabel = "Loading...";
            try {
                const resp = await fetch("/api/tts", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({text}),
                });
                const cloned = resp.clone();
                const maybeJson = await cloned.json().catch(() => null);
                if (maybeJson && maybeJson.error) {
                    alert("TTS error: " + maybeJson.error);
                    if (msg) msg.ttsLabel = "Play";
                    return;
                }
                const blob = await resp.blob();
                const audio = new Audio(URL.createObjectURL(blob));
                audio.play();
            } catch (e) {
                alert("TTS failed: " + e.message);
            } finally {
                if (msg) msg.ttsLabel = "Play";
            }
        },

        async toggleRecording() {
            if (this.isRecording) {
                this.mediaRecorder.stop();
                this.isRecording = false;
                return;
            }
            const stream = await navigator.mediaDevices.getUserMedia({audio: true});
            this.mediaRecorder = new MediaRecorder(stream);
            this.audioChunks = [];
            this.mediaRecorder.ondataavailable = (e) => this.audioChunks.push(e.data);
            this.mediaRecorder.onstop = async () => {
                const blob = new Blob(this.audioChunks, {type: "audio/webm"});
                const formData = new FormData();
                formData.append("file", blob, "recording.webm");
                const resp = await fetch("/api/transcribe", {method: "POST", body: formData});
                const data = await resp.json();
                if (data.error) {
                    alert("Transcription error: " + data.error);
                    return;
                }
                this.inputText = data.text;
            };
            this.mediaRecorder.start();
            this.isRecording = true;
        },

        // --- notes ---
        async loadNotes() {
            try {
                const resp = await fetch("/api/notes");
                const data = await resp.json();
                if (data.notes && data.notes.length > 0) {
                    this.notes = data.notes;
                } else {
                    // Server has no notes yet -- migrate any existing
                    // localStorage notes from before cross-device sync
                    // existed, so nothing already written gets lost.
                    const localNotes = JSON.parse(localStorage.getItem("athena_notes") || "[]");
                    if (localNotes.length > 0) {
                        this.notes = localNotes;
                        await this.saveNotesToStorage();
                        localStorage.removeItem("athena_notes");
                    }
                }
            } catch (e) {
                console.error("Failed to load notes:", e);
                this.notes = JSON.parse(localStorage.getItem("athena_notes") || "[]");
            }
        },
        async saveNotesToStorage() {
            try {
                await fetch("/api/notes", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({notes: this.notes}),
                });
            } catch (e) {
                console.error("Failed to save notes:", e);
            }
        },
        saveNotes() {
            const note = this.activeNote();
            if (note) note.updatedAt = Date.now();
            this.saveNotesToStorage();
        },
        noteWordCount() {
            const note = this.activeNote();
            if (!note || !note.body) return 0;
            return note.body.trim().split(/\s+/).filter(Boolean).length;
        },
        notePreviewText(note) {
            if (!note || !note.body) return "";
            return note.body.replace(/\s+/g, " ").trim().slice(0, 80);
        },
        insertNoteFormat(prefix, suffix, blockPrefix) {
            const ta = this.$refs.noteBody;
            const note = this.activeNote();
            if (!ta || !note) return;
            const start = ta.selectionStart;
            const end = ta.selectionEnd;
            const body = note.body || "";
            let insertion, newStart, newEnd;
            if (blockPrefix) {
                // Apply to every line touched by the selection, not just
                // the line the selection starts on -- a real list/heading
                // toggle has to persist across each selected line, and a
                // single stray prefix on one line surrounded by plain text
                // is also why it wasn't rendering as a real list in preview.
                const blockStart = body.lastIndexOf("\n", start - 1) + 1;
                let blockEnd = body.indexOf("\n", end);
                if (blockEnd === -1) blockEnd = body.length;
                const block = body.slice(blockStart, blockEnd);
                const lines = block.split("\n");
                const isOrdered = /^\d/.test(blockPrefix);
                const newLines = lines.map((line, i) => isOrdered ? `${i + 1}. ${line}` : `${blockPrefix}${line}`);
                const newBlock = newLines.join("\n");
                insertion = body.slice(0, blockStart) + newBlock + body.slice(blockEnd);
                newStart = start + (isOrdered ? 3 : blockPrefix.length);
                newEnd = end + (newBlock.length - block.length);
            } else {
                const selected = body.slice(start, end);
                insertion = body.slice(0, start) + prefix + selected + suffix + body.slice(end);
                newStart = start + prefix.length;
                newEnd = newStart + selected.length;
            }
            note.body = insertion;
            this.saveNotes();
            this.$nextTick(() => {
                ta.focus();
                ta.setSelectionRange(newStart, newEnd);
            });
        },

        handleNoteListContinue(e) {
            // Enter inside a list line should continue the list (next
            // number, same bullet) or, on an empty item, exit it --
            // without this, typing a second line after starting a list
            // never gets its own marker, and CommonMark's lazy-
            // continuation rule then silently folds that unmarked line
            // into the previous list item's own text instead of
            // starting a new one.
            const ta = this.$refs.noteBody;
            const note = this.activeNote();
            if (!ta || !note) return;
            const pos = ta.selectionStart;
            const body = note.body || "";
            const lineStart = body.lastIndexOf("\n", pos - 1) + 1;
            const beforeCursor = body.slice(lineStart, pos);
            const m = beforeCursor.match(/^(\s*)([-*]|\d+\.)\s(.*)$/);
            if (!m) return; // not on a list line -- let Enter behave normally
            e.preventDefault();
            const [, indent, marker, content] = m;
            if (content.trim() === "") {
                note.body = body.slice(0, lineStart) + body.slice(pos);
                this.saveNotes();
                this.$nextTick(() => {
                    ta.focus();
                    ta.setSelectionRange(lineStart, lineStart);
                });
                return;
            }
            const isOrdered = /^\d+\.$/.test(marker);
            const nextMarker = isOrdered ? `${parseInt(marker, 10) + 1}. ` : `${marker} `;
            const insertion = "\n" + indent + nextMarker;
            note.body = body.slice(0, pos) + insertion + body.slice(pos);
            const newPos = pos + insertion.length;
            this.saveNotes();
            this.$nextTick(() => {
                ta.focus();
                ta.setSelectionRange(newPos, newPos);
            });
        },
        newNote() {
            const note = {id: generateUUID(), title: "", body: "", createdAt: Date.now()};
            this.notes.unshift(note);
            this.activeNoteId = note.id;
            this.saveNotesToStorage();
        },
        activeNote() {
            return this.notes.find(n => n.id === this.activeNoteId) || null;
        },
        deleteNote(id) {
            if (!confirm("Delete this note?")) return;
            this.notes = this.notes.filter(n => n.id !== id);
            if (this.activeNoteId === id) this.activeNoteId = null;
            this.saveNotesToStorage();
        },

        // --- settings / data ---
        exportData() {
            const data = {
                sessions: this.sessions,
                notes: this.notes,
                exportedAt: new Date().toISOString(),
            };
            const blob = new Blob([JSON.stringify(data, null, 2)], {type: "application/json"});
            const a = document.createElement("a");
            a.href = URL.createObjectURL(blob);
            a.download = "athena-export.json";
            a.click();
        },
        async clearAllData() {
            if (!confirm("This deletes all local sessions and notes. Continue?")) return;
            localStorage.removeItem("athena_sessions");
            localStorage.removeItem("athena_notes");
            localStorage.removeItem("athena_session");
            try {
                await fetch("/api/notes", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({notes: []}),
                });
            } catch (e) {
                console.error("Failed to clear server-side notes:", e);
            }
            location.reload();
        },
    };
}
