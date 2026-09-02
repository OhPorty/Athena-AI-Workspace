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
            {id: "workspace", label: "Files", icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>'},
            {id: "memory", label: "Memory", icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9.5 2A2.5 2.5 0 0 1 12 4.5v15a2.5 2.5 0 0 1-4.96.5H7a2.5 2.5 0 0 1 0-5H8m4-11A2.5 2.5 0 0 1 14.5 2h.01A2.5 2.5 0 0 1 17 4.5v15a2.5 2.5 0 0 1-4.96.5"/></svg>'},
            {id: "skills", label: "Skills", icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2l2.4 7.2H22l-6 4.6 2.3 7.2-6.3-4.6-6.3 4.6 2.3-7.2-6-4.6h7.6z"/></svg>'},
            {id: "tasks", label: "Tasks", icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>'},
            {id: "notes", label: "Notes", icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z"/></svg>'},
            {id: "models", label: "Models", icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="8" rx="2"/><rect x="2" y="13" width="20" height="8" rx="2"/><line x1="6" y1="7" x2="6.01" y2="7"/><line x1="6" y1="17" x2="6.01" y2="17"/></svg>'},
            {id: "settings", label: "Settings", icon: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>'},
        ],

        // --- theme ---
        theme: localStorage.getItem("athena_theme") || "light",

        // --- chat / sessions ---
        sessionId: localStorage.getItem("athena_session") || generateUUID(),
        sessions: JSON.parse(localStorage.getItem("athena_sessions") || "[]"),
        sessionMenuOpen: null,
        messages: [],
        inputText: "",
        sending: false,
        isRecording: false,
        mediaRecorder: null,
        audioChunks: [],
        defaultModel: JSON.parse(localStorage.getItem("athena_default_model") || "null"),
        searchUrl: localStorage.getItem("athena_search_url") || "",
        usePi: localStorage.getItem("athena_use_pi") === "true",
        model: "",
        modelLabel: "Select a model",
        modelEndpointUrl: "",
        endpoints: JSON.parse(localStorage.getItem("athena_endpoints") || "[]"),
        addEndpointOpen: false,
        newEndpointType: "local",
        newEndpointName: "",
        newEndpointUrl: "",
        newEndpointProvider: "openai",
        newEndpointApiKey: "",
        maxCtx: 0,
        maxCtxText: "0",
        modelPopupOpen: false,
        workspace: localStorage.getItem("athena_workspace") || "",
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
        notes: JSON.parse(localStorage.getItem("athena_notes") || "[]"),
        activeNoteId: null,

        init() {
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
            if (!this.sessions.find(s => s.id === this.sessionId)) {
                this.sessions.unshift({id: this.sessionId, label: "New chat", createdAt: Date.now(), pinned: false});
                this.saveSessions();
            }

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
                    model: m.role === "assistant" ? this.getStoredMsgModel(m.messageId) : undefined,
                }));
                await this.loadRatingsForSession(id);
                this.scrollToBottom();
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
        saveSessions() {
            localStorage.setItem("athena_sessions", JSON.stringify(this.sessions));
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
            const pinned = filtered.filter(s => s.pinned);
            const rest = filtered.filter(s => !s.pinned);
            const groups = {Pinned: pinned, Today: [], Yesterday: [], Earlier: []};
            for (const s of rest) {
                const t = s.createdAt || 0;
                if (t >= today) groups.Today.push(s);
                else if (t >= yesterday) groups.Yesterday.push(s);
                else groups.Earlier.push(s);
            }
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
            if (s) s.pinned = !s.pinned;
            this.saveSessions();
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
            if (s && this.renameModalValue.trim()) s.label = this.renameModalValue.trim();
            this.saveSessions();
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
            this.saveSessions();
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
            localStorage.setItem("athena_search_url", this.searchUrl);
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
            localStorage.setItem("athena_default_model", JSON.stringify(this.defaultModel));
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
            localStorage.setItem("athena_endpoints", JSON.stringify(this.endpoints));
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

        scrollToBottom() {
            this.$nextTick(() => {
                if (this.$refs.chatBox) {
                    this.$refs.chatBox.scrollTop = this.$refs.chatBox.scrollHeight;
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

            this.messages.push({role: "user", content: text});
            let activeSession = this.sessions.find(s => s.id === this.sessionId);
            if (!activeSession) {
                activeSession = {id: this.sessionId, label: text.slice(0, 40), createdAt: Date.now(), pinned: false};
                this.sessions.unshift(activeSession);
                this.saveSessions();
            } else if (activeSession.label === "New chat") {
                activeSession.label = text.slice(0, 40);
                this.saveSessions();
            }
            this.scrollToBottom();

            const assistantMsg = {role: "assistant", content: "", thinking: "", thinkingOpen: true, ctxUsed: null, promptTokens: null, tokensPerSec: null, model: this.modelLabel, ttsLabel: "Play", toolCalls: [], toolsOpen: false, rating: null, messageId: null};
            this.messages.push(assistantMsg);
            const msgIndex = this.messages.length - 1;

            try {
                const resp = await fetch("/api/chat", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({
                        session_id: this.sessionId,
                        message: text,
                        model: this.model,
                        max_ctx: this.maxCtx,
                        workspace: this.workspace,
                        endpoint_url: this.modelEndpointUrl,
                        search_url: this.searchUrl,
                        use_pi: this.usePi,
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
                this.messages[msgIndex].content = "⚠️ Error: " + e.message;
            } finally {
                this.sending = false;
            }
        },

        toggleUsePi() {
            this.usePi = !this.usePi;
            localStorage.setItem("athena_use_pi", this.usePi);
        },

        selectWorkspace(path) {
            this.workspace = path;
            localStorage.setItem("athena_workspace", path);
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
        saveNotesToStorage() {
            localStorage.setItem("athena_notes", JSON.stringify(this.notes));
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
        clearAllData() {
            if (!confirm("This deletes all local sessions and notes. Continue?")) return;
            localStorage.removeItem("athena_sessions");
            localStorage.removeItem("athena_notes");
            localStorage.removeItem("athena_session");
            location.reload();
        },
    };
}
