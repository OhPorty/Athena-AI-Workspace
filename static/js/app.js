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
        pages: [
            {id: "chat", label: "Chat", icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>'},
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
        messages: [],
        inputText: "",
        sending: false,
        currentAbortController: null,
        isRecording: false,
        mediaRecorder: null,
        audioChunks: [],
        defaultModel: null,
        searchUrl: "",
        model: "",
        modelLabel: "Select a model",
        modelEndpointUrl: "",
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
            try {
                const resp = await fetch(`/api/history/${id}`);
                const history = await resp.json();
                this.messages = history.map(m => ({
                    ...m,
                    ttsLabel: "Play",
                    rating: null,
                    model: m.role === "assistant" ? (m.model || this.getStoredMsgModel(m.messageId)) : undefined,
                }));
                await this.loadRatingsForSession(id);
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
            } catch (e) {
                console.error("Failed to load session history:", e);
            }
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
                this.sessions = data.map(s => ({id: s.id, label: s.label, pinned: s.pinned, createdAt: s.created_at, lastActive: s.last_active}));
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
            return new Date(timestamp).toLocaleDateString(undefined, {month: "short", day: "numeric"});
        },

        groupedSessions() {
            const now = new Date();
            const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
            const yesterday = today - 86400000;
            const query = this.sessionSearch.trim().toLowerCase();
            const filtered = query
                ? this.sessions.filter(s => (s.label || "").toLowerCase().includes(query))
                : this.sessions;
            const activityTime = s => s.lastActive || s.createdAt || 0;
            const byRecent = (a, b) => activityTime(b) - activityTime(a);
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

        newChat() {
            this.sessionId = generateUUID();
            localStorage.setItem("athena_session", this.sessionId);
            this.messages = [];
        },

        async switchSession(id) {
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
        async confirmDelete() {
            const id = this.deleteConfirmSessionId;
            this.deleteConfirmSessionId = null;
            this.sessions = this.sessions.filter(x => x.id !== id);
            try {
                await fetch(`/api/history/${id}`, {method: "DELETE"});
            } catch (e) {
                console.error("Failed to delete session from LCM:", e);
            }
            if (id === this.sessionId) this.newChat();
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
            this.model = m.value;
            this.modelLabel = m.label;
            this.modelEndpointUrl = m.endpointUrl || "";
            this.modelPopupOpen = false;
        },

        saveSearchUrl() {
            fetch("/api/settings", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({search_url: this.searchUrl})}).catch(e => console.error("Failed to save search URL:", e));
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
                if (data.search_url !== undefined) this.searchUrl = data.search_url;
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
                        logoColor: reconstructLogo(t.light.logoHue),
                    },
                    dark: {
                        accent: t.dark.accent || t.light.accent, accentHover: t.dark.accentHover || t.light.accentHover,
                        accentIcon: t.dark.accentIcon || t.light.accentIcon, accentSoft: reconstructSoft(t.dark.accentSoft),
                        accentText: t.dark.accentText, accentTextStrong: t.dark.accentTextStrong,
                        accentBorder: t.dark.accentBorder, accentBorderFocus: t.dark.accentBorderFocus,
                        wordmark: t.dark.wordmark, logoColor: reconstructLogo(t.dark.logoHue),
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
                    wordmark: draft.wordmark, logoHue: logoHue, accentSoft: draft.accentSoft,
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
            };
            const dark = {
                accentSoft: `hsla(${h.toFixed(0)}, ${s.toFixed(0)}%, 60%, 0.1)`,
                accentText: this._hslToHex(h, s, 70),
                accentTextStrong: this._hslToHex(h, s, 80),
                accentBorder: this._hslToHex(h, s, 42),
                accentBorderFocus: this._hslToHex(h, s, 50),
                wordmark: this._hslToHex(h, Math.min(s + 10, 80), 65),
                logoHue: logoHue,
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
                if (ep.type !== "local") continue;
                for (const m of (ep.enabledModels || [])) {
                    list.push({value: m, label: `${m} (${ep.name})`, endpointUrl: ep.url});
                }
            }
            return list;
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
                provider: this.newEndpointProvider,
                apiKey: this.newEndpointApiKey,
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
                    body: JSON.stringify({url: ep.url}),
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
                    pre.appendChild(btn);
                });
            });
            return html;
        },

        autoGrow(e) {
            e.target.style.height = "auto";
            e.target.style.height = e.target.scrollHeight + "px";
        },

        _scrollThinkingToBottom() {
            this.$nextTick(() => {
                const boxes = document.querySelectorAll('.thinking-scroll-target');
                if (boxes.length) {
                    const last = boxes[boxes.length - 1];
                    last.scrollTop = last.scrollHeight;
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

            this.messages.push({role: "user", content: text, hasImage: imagesToSend.length > 0});
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

            const assistantMsg = {role: "assistant", content: "", thinking: "", thinkingOpen: true, ctxUsed: null, promptTokens: null, tokensPerSec: null, model: this.modelLabel, ttsLabel: "Play", toolCalls: [], toolsOpen: false, rating: null, messageId: null};
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
                            this.messages[msgIndex].content += data.delta;
                            this.messages[msgIndex].thinkingOpen = false;
                            this.scrollToBottom();
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
            this.saveNotesToStorage();
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
