import { useState, useRef, useEffect, useCallback, type FormEvent } from "react";
import "./index.css";

const API_BASE = "http://localhost:8000/api";

interface Message {
    role: "user" | "assistant" | "system";
    content: string;
    toolCalls?: string[];
    metrics?: {
        latency_seconds: number;
        input_tokens: number;
        output_tokens: number;
        tokens_per_second: number | null;
    };
}

interface Command {
    label: string;
    description: string;
    action: () => Promise<string>;
}

interface ReportMeta {
    file_name: string;
    report_path: string;
    size_bytes: number;
    modified_at: string;
}

type View = "chat" | "reports" | "sessions";

export function App() {
    const [messages, setMessages] = useState<Message[]>([]);
    const [input, setInput] = useState("");
    const [loading, setLoading] = useState(false);
    const [sessionId, setSessionId] = useState<string | null>(null);
    const [menuOpen, setMenuOpen] = useState(false);

    // View state
    const [view, setView] = useState<View>("chat");

    // Reports state
    const [reports, setReports] = useState<ReportMeta[]>([]);
    const [reportsLoading, setReportsLoading] = useState(false);
    const [selectedReport, setSelectedReport] = useState<string | null>(null);
    const [reportContent, setReportContent] = useState<string | null>(null);
    const [reportContentLoading, setReportContentLoading] = useState(false);

    // Sessions state
    const [sessions, setSessions] = useState<string[]>([]);
    const [sessionsLoading, setSessionsLoading] = useState(false);

    const messagesRef = useRef<HTMLDivElement>(null);
    const inputRef = useRef<HTMLTextAreaElement>(null);
    const menuRef = useRef<HTMLDivElement>(null);

    const scrollToBottom = useCallback(() => {
        const el = messagesRef.current;
        if (el) el.scrollTop = el.scrollHeight;
    }, []);

    useEffect(() => {
        scrollToBottom();
    }, [messages, loading, scrollToBottom]);

    // Close menu on outside click
    useEffect(() => {
        const handler = (e: MouseEvent) => {
            if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
                setMenuOpen(false);
            }
        };
        if (menuOpen) document.addEventListener("mousedown", handler);
        return () => document.removeEventListener("mousedown", handler);
    }, [menuOpen]);

    const addMessage = (msg: Message) => {
        setMessages((prev) => [...prev, msg]);
    };

    const sendChat = async (text: string) => {
        const userMsg: Message = { role: "user", content: text };
        addMessage(userMsg);
        setLoading(true);

        try {
            const res = await fetch(`${API_BASE}/chat`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    message: text,
                    session_id: sessionId,
                }),
            });

            const data = await res.json();

            if (!data.ok) {
                addMessage({ role: "system", content: `Error: ${data.error}` });
                return;
            }

            if (!sessionId && data.session_id) {
                setSessionId(data.session_id);
            }

            const assistantMsg: Message = {
                role: "assistant",
                content: data.response.trim(),
                toolCalls: data.tool_calls?.length ? data.tool_calls : undefined,
                metrics: data.metrics,
            };
            addMessage(assistantMsg);
        } catch (err) {
            addMessage({
                role: "system",
                content: `Connection error: ${err instanceof Error ? err.message : "Unknown error"}`,
            });
        } finally {
            setLoading(false);
        }
    };

    const handleSubmit = (e: FormEvent) => {
        e.preventDefault();
        const text = input.trim();
        if (!text || loading) return;
        setInput("");
        sendChat(text);
    };

    const handleKeyDown = (e: React.KeyboardEvent) => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            handleSubmit(e);
        }
    };

    const resetSession = () => {
        setSessionId(null);
        setMessages([]);
        setView("chat");
    };

    // --- Reports ---

    const fetchReports = async () => {
        setReportsLoading(true);
        try {
            const res = await fetch(`${API_BASE}/reports?limit=50`);
            const data = await res.json();
            if (data.ok) {
                setReports(data.reports);
            }
        } catch {
            // silently fail
        } finally {
            setReportsLoading(false);
        }
    };

    const openReportsView = () => {
        setView("reports");
        setSelectedReport(null);
        setReportContent(null);
        fetchReports();
    };

    const viewReport = async (fileName: string) => {
        setSelectedReport(fileName);
        setReportContent(null);
        setReportContentLoading(true);
        try {
            const res = await fetch(
                `${API_BASE}/reports/${encodeURIComponent(fileName)}?include_content=true`
            );
            const data = await res.json();
            if (data.ok && data.report_content) {
                setReportContent(data.report_content);
            } else {
                setReportContent("Failed to load report content.");
            }
        } catch {
            setReportContent("Failed to load report.");
        } finally {
            setReportContentLoading(false);
        }
    };

    // --- Sessions ---

    const fetchSessions = async () => {
        setSessionsLoading(true);
        try {
            const res = await fetch(`${API_BASE}/sessions`);
            const data = await res.json();
            if (data.ok) {
                setSessions(data.sessions);
            }
        } catch {
            // silently fail
        } finally {
            setSessionsLoading(false);
        }
    };

    const openSessionsView = () => {
        setView("sessions");
        fetchSessions();
    };

    const switchToSession = async (sid: string) => {
        try {
            const res = await fetch(`${API_BASE}/sessions/${encodeURIComponent(sid)}`);
            const data = await res.json();
            if (data.ok) {
                setSessionId(sid);
                setMessages(
                    data.messages.map((m: { role: string; content: string }) => ({
                        role: m.role as Message["role"],
                        content: m.content,
                    }))
                );
                setView("chat");
            }
        } catch {
            // silently fail
        }
    };

    // --- Commands ---

    const commands: Command[] = [
        {
            label: "Generate Report",
            description: "Generate a weekly health report",
            action: async () => {
                const res = await fetch(`${API_BASE}/report`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ days: 7 }),
                });
                const data = await res.json();
                if (!data.ok) return `Error: ${data.error}`;
                return `Report generated: ${data.report_path}`;
            },
        },
        {
            label: "Sync CGM DB",
            description: "Sync LibreLink CGM readings into SQLite",
            action: async () => {
                const res = await fetch(`${API_BASE}/cgm/sync`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ mode: "latest" }),
                });
                const data = await res.json();
                if (!data.ok) return `Error: ${data.error}`;
                return data.result;
            },
        },
        {
            label: "Run CGM Enrichment",
            description: "Process deferred CGM log enrichments",
            action: async () => {
                const res = await fetch(`${API_BASE}/cgm/enrichment/run`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ limit: 50 }),
                });
                const data = await res.json();
                if (!data.ok) return `Error: ${data.error}`;
                return data.result;
            },
        },
        {
            label: "ICR Summary",
            description: "Insulin-to-carb ratio analysis",
            action: async () => {
                const res = await fetch(`${API_BASE}/kb/icr-summary`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ days: 14 }),
                });
                const data = await res.json();
                if (!data.ok) return `Error: ${data.error}`;
                return data.result;
            },
        },
        {
            label: "Sync Glucose to KB",
            description: "Sync glucose readings into knowledge base",
            action: async () => {
                const res = await fetch(`${API_BASE}/kb/sync-glucose`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ days: 7 }),
                });
                const data = await res.json();
                if (!data.ok) return `Error: ${data.error}`;
                return data.result;
            },
        },
    ];

    const runCommand = async (cmd: Command) => {
        setMenuOpen(false);
        addMessage({ role: "system", content: `Running: ${cmd.label}...` });
        setLoading(true);

        try {
            const result = await cmd.action();
            addMessage({ role: "assistant", content: result });
        } catch (err) {
            addMessage({
                role: "system",
                content: `Command failed: ${err instanceof Error ? err.message : "Unknown error"}`,
            });
        } finally {
            setLoading(false);
        }
    };

    const autoResize = (el: HTMLTextAreaElement) => {
        el.style.height = "auto";
        el.style.height = Math.min(el.scrollHeight, 120) + "px";
    };

    const formatDate = (iso: string) => {
        try {
            const d = new Date(iso);
            return d.toLocaleDateString(undefined, {
                month: "short",
                day: "numeric",
                year: "numeric",
                hour: "2-digit",
                minute: "2-digit",
            });
        } catch {
            return iso;
        }
    };

    const formatSize = (bytes: number) => {
        if (bytes < 1024) return `${bytes} B`;
        return `${(bytes / 1024).toFixed(1)} KB`;
    };

    // --- Render ---

    return (
        <div className="app">
            <div className="header">
                <span
                    className="header-title"
                    onClick={() => setView("chat")}
                    style={{ cursor: "pointer" }}
                >
                    Jetson
                </span>
                <div className="header-actions">
                    <button
                        className={`header-btn ${view === "sessions" ? "header-btn-active" : ""}`}
                        onClick={openSessionsView}
                    >
                        History
                    </button>
                    <button
                        className={`header-btn ${view === "reports" ? "header-btn-active" : ""}`}
                        onClick={openReportsView}
                    >
                        Reports
                    </button>
                    <button className="header-btn" onClick={resetSession}>
                        New Chat
                    </button>
                </div>
            </div>

            {view === "reports" ? (
                <div className="panel">
                    {selectedReport ? (
                        <>
                            <div className="panel-header">
                                <button
                                    className="panel-back"
                                    onClick={() => {
                                        setSelectedReport(null);
                                        setReportContent(null);
                                    }}
                                >
                                    Back
                                </button>
                                <span className="panel-title">{selectedReport}</span>
                            </div>
                            <div className="panel-content">
                                {reportContentLoading ? (
                                    <div className="panel-loading">Loading report...</div>
                                ) : (
                                    <pre className="report-text">{reportContent}</pre>
                                )}
                            </div>
                        </>
                    ) : (
                        <>
                            <div className="panel-header">
                                <span className="panel-title">Reports</span>
                            </div>
                            <div className="panel-content">
                                {reportsLoading ? (
                                    <div className="panel-loading">Loading reports...</div>
                                ) : reports.length === 0 ? (
                                    <div className="panel-empty">No reports yet. Use the Generate Report command to create one.</div>
                                ) : (
                                    <div className="panel-list">
                                        {reports.map((r) => (
                                            <button
                                                key={r.file_name}
                                                className="panel-list-item"
                                                onClick={() => viewReport(r.file_name)}
                                            >
                                                <div className="panel-list-item-title">
                                                    {r.file_name}
                                                </div>
                                                <div className="panel-list-item-meta">
                                                    {formatDate(r.modified_at)} &middot; {formatSize(r.size_bytes)}
                                                </div>
                                            </button>
                                        ))}
                                    </div>
                                )}
                            </div>
                        </>
                    )}
                </div>
            ) : view === "sessions" ? (
                <div className="panel">
                    <div className="panel-header">
                        <span className="panel-title">Chat History</span>
                    </div>
                    <div className="panel-content">
                        {sessionsLoading ? (
                            <div className="panel-loading">Loading sessions...</div>
                        ) : sessions.length === 0 ? (
                            <div className="panel-empty">No previous sessions.</div>
                        ) : (
                            <div className="panel-list">
                                {sessions.map((sid) => (
                                    <button
                                        key={sid}
                                        className={`panel-list-item ${sid === sessionId ? "panel-list-item-active" : ""}`}
                                        onClick={() => switchToSession(sid)}
                                    >
                                        <div className="panel-list-item-title">
                                            {sid.slice(0, 8)}...
                                        </div>
                                        <div className="panel-list-item-meta">
                                            {sid === sessionId ? "Current session" : "Click to resume"}
                                        </div>
                                    </button>
                                ))}
                            </div>
                        )}
                    </div>
                </div>
            ) : (
                <>
                    {messages.length === 0 && !loading ? (
                        <div className="empty-state">
                            <div className="empty-state-title">Jetson</div>
                            <div className="empty-state-subtitle">
                                Your diabetes management assistant
                            </div>
                        </div>
                    ) : (
                        <div className="messages" ref={messagesRef}>
                            {messages.map((msg, i) => (
                                <div key={i} className={`message message-${msg.role}`}>
                                    <div>{msg.content}</div>
                                    {msg.metrics && (
                                        <div className="message-meta">
                                            <span>{msg.metrics.latency_seconds.toFixed(1)}s</span>
                                            <span>{msg.metrics.input_tokens + msg.metrics.output_tokens} tokens</span>
                                            {msg.toolCalls && msg.toolCalls.length > 0 && (
                                                <span>tools: {msg.toolCalls.join(", ")}</span>
                                            )}
                                        </div>
                                    )}
                                </div>
                            ))}
                            {loading && (
                                <div className="typing">
                                    Thinking<span className="typing-dots"></span>
                                </div>
                            )}
                        </div>
                    )}

                    <div className="input-area">
                        <div className="command-bar">
                            <div ref={menuRef} style={{ position: "relative" }}>
                                <button
                                    className="command-trigger"
                                    onClick={() => setMenuOpen(!menuOpen)}
                                >
                                    Commands
                                </button>
                                {menuOpen && (
                                    <div className="command-menu">
                                        {commands.map((cmd) => (
                                            <button
                                                key={cmd.label}
                                                className="command-item"
                                                onClick={() => runCommand(cmd)}
                                            >
                                                <div className="command-item-label">{cmd.label}</div>
                                                <div className="command-item-desc">{cmd.description}</div>
                                            </button>
                                        ))}
                                    </div>
                                )}
                            </div>
                        </div>
                        <form className="input-row" onSubmit={handleSubmit}>
                            <textarea
                                ref={inputRef}
                                className="input-field"
                                value={input}
                                onChange={(e) => {
                                    setInput(e.target.value);
                                    autoResize(e.target);
                                }}
                                onKeyDown={handleKeyDown}
                                placeholder="Message Jetson..."
                                rows={1}
                                disabled={loading}
                            />
                            <button
                                type="submit"
                                className="send-btn"
                                disabled={!input.trim() || loading}
                            >
                                Send
                            </button>
                        </form>
                    </div>
                </>
            )}
        </div>
    );
}

export default App;
