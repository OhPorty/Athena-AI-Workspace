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
        workspace: "",
        workspacePopupOpen: false,

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

        groupedSessions() {
            const now = new Date();
            const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
            const yesterday = today - 86400000;
            const pinned = this.sessions.filter(s => s.pinned);
            const rest = this.sessions.filter(s => !s.pinned);
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
            this.sessions.unshift({id: this.sessionId, label: "New chat", createdAt: Date.now(), pinned: false});
            this.saveSessions();
            this.messages = [];
        },

        async switchSession(id) {
            this.sessionId = id;
            localStorage.setItem("athena_session", id);
            this.messages = [];
            this.sessionMenuOpen = null;
            try {
                const resp = await fetch(`/api/history/${id}`);
                const history = await resp.json();
                this.messages = history.map(m => ({...m, ttsLabel: "Play"}));
                this.scrollToBottom();
            } catch (e) {
                console.error("Failed to load session history:", e);
            }
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
            const newName = prompt("Rename chat", s.label);
            if (newName && newName.trim()) s.label = newName.trim();
            this.saveSessions();
            this.sessionMenuOpen = null;
        },

        deleteSession(id) {
            if (!confirm("Delete this chat?")) return;
            this.sessions = this.sessions.filter(x => x.id !== id);
            this.saveSessions();
            if (id === this.sessionId) this.newChat();
            this.sessionMenuOpen = null;
        },

        selectModel(m) {
            this.model = m.value;
            this.modelLabel = m.label;
            this.modelEndpointUrl = m.endpointUrl || "";
            this.modelPopupOpen = false;
        },

        setAsDefaultModel() {
            if (!this.model) return;
            this.defaultModel = {value: this.model, label: this.modelLabel, endpointUrl: this.modelEndpointUrl};
            localStorage.setItem("athena_default_model", JSON.stringify(this.defaultModel));
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
            if (!text || this.sending) return;
            this.inputText = "";
            this.sending = true;

            this.messages.push({role: "user", content: text});
            const activeSession = this.sessions.find(s => s.id === this.sessionId);
            if (activeSession && activeSession.label === "New chat") {
                activeSession.label = text.slice(0, 40);
                this.saveSessions();
            }
            this.scrollToBottom();

            const assistantMsg = {role: "assistant", content: "", thinking: "", thinkingOpen: true, ctxUsed: null, promptTokens: null, ttsLabel: "Play", toolCalls: []};
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
                        }
                    }
                }
            } catch (e) {
                this.messages[msgIndex].content = "⚠️ Error: " + e.message;
            } finally {
                this.sending = false;
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
