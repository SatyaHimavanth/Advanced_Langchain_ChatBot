import { useEffect, useState, useRef } from 'react';
import { useAuth } from '../components/AuthContext';
import {
    MessageSquare, Plus, Trash2, Edit2,
    Archive, LogOut, Send, Bot, User as UserIcon, AlertTriangle,
    Moon, Sun, ChevronDown, RefreshCw, ArrowLeft
} from 'lucide-react';

function Dashboard() {
    const { token, logout, apiFetch } = useAuth();

    const [histories, setHistories] = useState([]);
    const [activeHistory, setActiveHistory] = useState(null);

    const [messages, setMessages] = useState([]);
    const [input, setInput] = useState('');
    const [loading, setLoading] = useState(false);

    const [pendingReview, setPendingReview] = useState(null);
    const [editedArgsText, setEditedArgsText] = useState('');
    const [reviewLoading, setReviewLoading] = useState(false);

    // New UI features
    const [isDarkMode, setIsDarkMode] = useState(true);
    const [isProfileOpen, setIsProfileOpen] = useState(false);
    const [viewMode, setViewMode] = useState('active'); // active or archived

    const messagesEndRef = useRef(null);
    const threadId = useRef(`thread-${Date.now()}`);

    const getUsername = () => {
        if (!token) return 'User';
        try {
            const payload = JSON.parse(atob(token.split('.')[1]));
            const sub = payload.sub || 'User';
            return sub.length > 6 ? sub.substring(0, 6) + '...' : sub;
        } catch (e) {
            return 'User';
        }
    };

    useEffect(() => {
        if (isDarkMode) {
            document.body.classList.remove('light-theme');
        } else {
            document.body.classList.add('light-theme');
        }
    }, [isDarkMode]);

    const scrollToBottom = () => {
        messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
    };

    useEffect(() => {
        scrollToBottom();
    }, [messages, pendingReview]);

    useEffect(() => {
        fetchHistories();
    }, [viewMode]);

    const fetchHistories = async () => {
        try {
            const endpoint = viewMode === 'archived' ? '/history/archived' : '/history/';
            const res = await apiFetch(endpoint);
            if (res.ok) {
                const data = await res.json();
                setHistories(data);
                if (activeHistory && !data.find(h => h.id === activeHistory)) {
                    setActiveHistory(null);
                    setMessages([]);
                }
            }
        } catch (e) {
            console.error(e);
        }
    };

    const createNewChat = async () => {
        if (viewMode === 'archived') {
            setViewMode('active');
        }
        try {
            const res = await apiFetch('/history/', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ title: 'New Conversation' })
            });
            if (res.ok) {
                const data = await res.json();
                setHistories([data, ...histories]);
                setActiveHistory(data.id);
                setMessages([{ role: 'assistant', text: 'How can I assist you with your files today?' }]);
                threadId.current = `thread-${Date.now()}`;
            }
        } catch (e) {
            console.error(e);
        }
    };

    const loadHistory = async (id) => {
        try {
            const res = await apiFetch(`/history/${id}`);
            if (res.ok) {
                const data = await res.json();
                setActiveHistory(id);
                setMessages(data.messages.length > 0 ? data.messages : [{ role: 'assistant', text: 'How can I assist you with your files today?' }]);
                threadId.current = `thread-${id}`;
            }
        } catch (e) {
            console.error(e);
        }
    };

    const deleteHistory = async (e, id) => {
        e.stopPropagation();
        try {
            await apiFetch(`/history/${id}`, {
                method: 'DELETE'
            });
            setHistories(histories.filter(h => h.id !== id));
            if (activeHistory === id) {
                setActiveHistory(null);
                setMessages([]);
            }
        } catch (err) {
            console.error(err);
        }
    };

    const renameHistory = async (e, id, currentTitle) => {
        e.stopPropagation();
        const newTitle = prompt('Enter new name:', currentTitle);
        if (!newTitle) return;

        try {
            await apiFetch(`/history/${id}`, {
                method: 'PUT',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ title: newTitle })
            });
            fetchHistories();
        } catch (err) {
            console.error(err);
        }
    };

    const processArchive = async (e, id, action) => {
        e.stopPropagation();
        try {
            await apiFetch(`/history/${id}/${action}`, {
                method: 'PATCH'
            });
            setHistories(histories.filter(h => h.id !== id));
            if (activeHistory === id) {
                setActiveHistory(null);
                setMessages([]);
            }
        } catch (err) {
            console.error(err);
        }
    };

    // Chat logics
    async function postJson(path, payload) {
        const res = await apiFetch(path, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(payload),
        });

        const data = await res.json();
        if (!res.ok) {
            throw new Error(data?.detail || 'Request failed');
        }
        return data;
    }

    function handleAgentResponse(data) {
        if (data.type === 'assistant') {
            setMessages(prev => [...prev, { role: 'assistant', text: data.message || '(no response)' }]);
            setPendingReview(null);
            setEditedArgsText('');

            // Auto-rename if it's "New Conversation" and we have our first exchange
            const history = histories.find(h => h.id === activeHistory);
            if (history && history.title === 'New Conversation' && messages.length <= 2) {
                // Optionally you could call an LLM to generate a title here, or just use first 20 chars of user input
            }
            return;
        }

        if (data.type === 'review_required') {
            setPendingReview(data.review);
            setEditedArgsText(JSON.stringify(data.review.args, null, 2));
        }
    }

    const sendMessage = async (e) => {
        e.preventDefault();
        const text = input.trim();
        if (!text || loading || reviewLoading || pendingReview || viewMode === 'archived') return;

        if (!activeHistory) {
            await createNewChat();
        }

        setMessages(prev => [...prev, { role: 'user', text }]);
        setInput('');
        setLoading(true);

        try {
            const payload = {
                thread_id: threadId.current,
                message: text,
                history_id: activeHistory
            };
            const data = await postJson('/chat', payload);
            handleAgentResponse(data);
        } catch (err) {
            setMessages(prev => [...prev, { role: 'system', text: `Error: ${err.message}` }]);
        } finally {
            setLoading(false);
        }
    };

    async function submitDecision(decision) {
        if (!pendingReview || loading || reviewLoading) return;

        let editedArgs = undefined;
        if (decision === 'edit') {
            try {
                editedArgs = JSON.parse(editedArgsText);
            } catch {
                setMessages(prev => [...prev, { role: 'system', text: 'Error: edited args must be valid JSON.' }]);
                return;
            }
        }

        setReviewLoading(true);
        try {
            const data = await postJson('/review', {
                thread_id: threadId.current,
                decision,
                edited_args: editedArgs,
            });
            handleAgentResponse(data);
        } catch (err) {
            setMessages(prev => [...prev, { role: 'system', text: `Error: ${err.message}` }]);
        } finally {
            setReviewLoading(false);
        }
    }

    // Click outside listener for profile dropdown
    useEffect(() => {
        const handleClickOutside = (event) => {
            if (isProfileOpen && !event.target.closest('.user-profile')) {
                setIsProfileOpen(false);
            }
        };
        document.addEventListener('click', handleClickOutside);
        return () => document.removeEventListener('click', handleClickOutside);
    }, [isProfileOpen]);

    return (
        <div className="dashboard-layout">
            <button
                className="theme-toggle"
                onClick={() => setIsDarkMode(!isDarkMode)}
                title={isDarkMode ? "Switch to Light Mode" : "Switch to Dark Mode"}
            >
                {isDarkMode ? <Sun size={20} /> : <Moon size={20} />}
            </button>

            <aside className="sidebar">
                <div className="sidebar-header">
                    <div className="logo-container" style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                        <Bot size={24} color="var(--accent-color)" />
                        <h2 style={{ fontSize: '18px' }}>My Agent</h2>
                    </div>
                </div>

                {viewMode === 'active' ? (
                    <button className="btn-primary new-chat-btn" onClick={createNewChat}>
                        <Plus size={18} /> New Agent Task
                    </button>
                ) : (
                    <button className="btn-primary new-chat-btn" onClick={() => setViewMode('active')} style={{ background: 'var(--bg-panel)', border: '1px solid var(--border-glass)' }}>
                        <ArrowLeft size={18} /> Back to Active
                    </button>
                )}

                <div className="history-list">
                    {histories.length === 0 && (
                        <div style={{ textAlign: 'center', padding: '20px', color: 'var(--text-muted)', fontSize: '14px' }}>
                            No {viewMode} chats found
                        </div>
                    )}
                    {histories.map(h => (
                        <div
                            key={h.id}
                            className={`history-item ${activeHistory === h.id ? 'active' : ''}`}
                            onClick={() => loadHistory(h.id)}
                        >
                            <div className="history-item-content">
                                <MessageSquare size={16} />
                                <span className="history-title">{h.title}</span>
                            </div>
                            <div className="history-actions">
                                {viewMode === 'active' ? (
                                    <>
                                        <button onClick={(e) => renameHistory(e, h.id, h.title)} title="Rename"><Edit2 size={14} /></button>
                                        <button onClick={(e) => processArchive(e, h.id, 'archive')} title="Archive"><Archive size={14} /></button>
                                    </>
                                ) : (
                                    <button onClick={(e) => processArchive(e, h.id, 'unarchive')} title="Unarchive"><RefreshCw size={14} /></button>
                                )}
                                <button onClick={(e) => deleteHistory(e, h.id)} title="Delete"><Trash2 size={14} /></button>
                            </div>
                        </div>
                    ))}
                </div>

                <div className="user-profile" onClick={() => setIsProfileOpen(!isProfileOpen)}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                        <div className="avatar">
                            <UserIcon size={20} color="white" />
                        </div>
                        <span style={{ fontWeight: 500, fontSize: '14px' }}>{getUsername()}</span>
                    </div>
                    <ChevronDown size={18} color="var(--text-muted)" style={{ transform: isProfileOpen ? 'rotate(180deg)' : 'none', transition: 'transform 0.2s ' }} />

                    {isProfileOpen && (
                        <div className="profile-dropdown animate-fade-in" onClick={(e) => e.stopPropagation()}>
                            {viewMode === 'active' ? (
                                <button className="dropdown-item" onClick={() => { setViewMode('archived'); setIsProfileOpen(false); }}>
                                    <Archive size={16} /> View Archived
                                </button>
                            ) : (
                                <button className="dropdown-item" onClick={() => { setViewMode('active'); setIsProfileOpen(false); }}>
                                    <MessageSquare size={16} /> View Active
                                </button>
                            )}
                            <button className="dropdown-item text-danger" onClick={logout}>
                                <LogOut size={16} /> Logout
                            </button>
                        </div>
                    )}
                </div>
            </aside>

            <main className="main-content">
                <header className="chat-header">
                    <h2>
                        {viewMode === 'archived' && <span style={{ color: 'var(--text-muted)', marginRight: '8px' }}>[Archived]</span>}
                        {histories.find(h => h.id === activeHistory)?.title || 'Select or start a new task'}
                    </h2>
                </header>

                <div className="chat-container">
                    {messages.map((msg, idx) => (
                        <div key={idx} className={`message-wrapper ${msg.role}`}>
                            <div className="message-bubble">
                                <pre style={{ fontFamily: 'inherit', whiteSpace: 'pre-wrap' }}>{msg.text}</pre>
                            </div>
                        </div>
                    ))}

                    {loading && (
                        <div className="message-wrapper assistant">
                            <div className="message-bubble" style={{ display: 'flex', gap: '8px' }}>
                                <span className="typing-dot" style={{ animation: 'fadeIn 1s infinite alternate' }}>•</span>
                                <span className="typing-dot" style={{ animation: 'fadeIn 1s infinite alternate 0.2s' }}>•</span>
                                <span className="typing-dot" style={{ animation: 'fadeIn 1s infinite alternate 0.4s' }}>•</span>
                            </div>
                        </div>
                    )}

                    {pendingReview && (
                        <div className="review-card animate-fade-in">
                            <div className="review-header">
                                <AlertTriangle size={24} />
                                <h3>Action Requires Permission</h3>
                            </div>

                            <div style={{ marginBottom: '16px' }}>
                                <p style={{ color: 'var(--text-muted)' }}>Tool executing:</p>
                                <p style={{ fontWeight: 600, fontSize: '16px', margin: '4px 0 12px' }}>{pendingReview.tool_name}</p>
                                <p>{pendingReview.description}</p>
                            </div>

                            {pendingReview.allowed_decisions.includes('edit') && (
                                <div style={{ marginTop: '16px' }}>
                                    <label style={{ display: 'block', marginBottom: '8px', color: 'var(--text-muted)' }}>Review Arguments (JSON)</label>
                                    <textarea
                                        value={editedArgsText}
                                        onChange={(e) => setEditedArgsText(e.target.value)}
                                        rows={6}
                                        style={{ width: '100%', background: 'rgba(0,0,0,0.3)', border: '1px solid var(--border-glass)', borderRadius: '8px', padding: '12px', color: 'white', fontFamily: 'monospace' }}
                                    />
                                </div>
                            )}

                            <div className="review-actions">
                                {pendingReview.allowed_decisions.includes('approve') && (
                                    <button className="btn-primary btn-approve" disabled={reviewLoading} onClick={() => submitDecision('approve')}>
                                        Approve Action
                                    </button>
                                )}
                                {pendingReview.allowed_decisions.includes('edit') && (
                                    <button className="btn-primary btn-edit" disabled={reviewLoading} onClick={() => submitDecision('edit')}>
                                        Save & Proceed
                                    </button>
                                )}
                                {pendingReview.allowed_decisions.includes('reject') && (
                                    <button className="btn-primary btn-reject" disabled={reviewLoading} onClick={() => submitDecision('reject')}>
                                        Reject Action
                                    </button>
                                )}
                            </div>
                        </div>
                    )}
                    <div ref={messagesEndRef} />
                </div>

                <div className="input-area">
                    <form className="input-form" onSubmit={sendMessage}>
                        <input
                            type="text"
                            className="chat-input"
                            value={input}
                            onChange={(e) => setInput(e.target.value)}
                            placeholder={viewMode === 'archived' ? "Cannot send messages in archived chats" : "Command your agent..."}
                            disabled={loading || reviewLoading || !!pendingReview || viewMode === 'archived'}
                        />
                        <button
                            type="submit"
                            className="send-btn"
                            disabled={loading || reviewLoading || !!pendingReview || !input.trim() || viewMode === 'archived'}
                            title={viewMode === 'archived' ? "Cannot send messages in archived chats" : "Send message"}
                        >
                            <Send size={18} />
                        </button>
                    </form>
                </div>
            </main>
        </div>
    );
}

export default Dashboard;
