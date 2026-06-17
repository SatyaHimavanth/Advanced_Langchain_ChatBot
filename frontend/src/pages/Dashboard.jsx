import { useEffect, useState, useRef, useCallback } from 'react';
import { useAuth } from '../components/AuthContext';
import {
    MessageSquare, Plus, Trash2, Edit2,
    Archive, LogOut, Send, Bot, User as UserIcon,
    Moon, Sun, ChevronDown, RefreshCw, ArrowLeft, Square,
    MoreVertical, Pin, PinOff, Settings, Zap, ChevronsLeft, ChevronsRight
} from 'lucide-react';
import AgentTurn from '../components/AgentTurn';
import InterruptCard from '../components/InterruptCard';
import ArtifactPanel from '../components/ArtifactPanel';
import { ArtifactProvider, useArtifact } from '../components/ArtifactContext';
import { streamChat, reduceEvent, splitFinal } from '../lib/agentStream';

function DashboardInner() {
    const { artifact } = useArtifact();
    const { token, logout, apiFetch } = useAuth();

    // App configuration (for pending user info)
    const [appConfig, setAppConfig] = useState({ pending_user_expire_days: 7 });

    // Chat history with pagination
    const [histories, setHistories] = useState([]);
    const [pinnedChats, setPinnedChats] = useState([]);
    const [historyOffset, setHistoryOffset] = useState(0);
    const [hasMoreHistory, setHasMoreHistory] = useState(true);
    const [loadingMore, setLoadingMore] = useState(false);
    const [activeHistory, setActiveHistory] = useState(null);

    // Messages with pagination
    const [messages, setMessages] = useState([]);
    const [messageOffset, setMessageOffset] = useState(0);
    const [hasMoreMessages, setHasMoreMessages] = useState(false);
    const [loadingMoreMessages, setLoadingMoreMessages] = useState(false);

    const [input, setInput] = useState('');
    const [loading, setLoading] = useState(false);

    // Model selection
    const [models, setModels] = useState([]);
    const [selectedModel, setSelectedModel] = useState('');
    const [defaultModel, setDefaultModel] = useState('');
    const [userQuota, setUserQuota] = useState(null);
    const [showModelDropdown, setShowModelDropdown] = useState(false);

    // Live streaming turn: { blocks, finalText, meta, error, interrupt } | null
    const [streamingTurn, setStreamingTurn] = useState(null);
    // The conversation id the active stream belongs to (number) | null.
    const [streamHistoryId, setStreamHistoryId] = useState(null);
    // Pending HITL approval: { historyId, data } | null
    const [pendingInterrupt, setPendingInterrupt] = useState(null);

    const [isDarkMode, setIsDarkMode] = useState(true);
    const [isProfileOpen, setIsProfileOpen] = useState(false);
    const [viewMode, setViewMode] = useState('active'); // active | archived
    const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
    
    // Actions menu state
    const [openMenuId, setOpenMenuId] = useState(null);

    const messagesEndRef = useRef(null);
    const messagesContainerRef = useRef(null);
    const historyListRef = useRef(null);
    const abortRef = useRef(null);
    const streamRestoreTimerRef = useRef(null);
    const inputRef = useRef(null);
    const modelDropdownRef = useRef(null);
    // Mirror of activeHistory for use inside async stream callbacks (avoids
    // stale closures when the user switches conversations mid-stream).
    const activeHistoryRef = useRef(null);
    useEffect(() => { activeHistoryRef.current = activeHistory; }, [activeHistory]);

    const stopRestoredStreamPolling = useCallback(() => {
        if (streamRestoreTimerRef.current) {
            clearTimeout(streamRestoreTimerRef.current);
            streamRestoreTimerRef.current = null;
        }
    }, []);

    const getUserInfo = () => {
        if (!token) return { username: 'User', role: 'user' };
        try {
            const payload = JSON.parse(atob(token.split('.')[1]));
            const sub = payload.sub || 'User';
            const role = payload.role || 'user';
            return {
                username: sub.length > 12 ? sub.substring(0, 12) + '…' : sub,
                role,
            };
        } catch {
            return { username: 'User', role: 'user' };
        }
    };
    
    const userInfo = getUserInfo();
    const isAdmin = userInfo.role === 'admin';
    const isPending = userInfo.role === 'pending';

    useEffect(() => {
        document.body.classList.toggle('light-theme', !isDarkMode);
    }, [isDarkMode]);

    // Fetch app config for pending users
    useEffect(() => {
        if (isPending) {
            const fetchConfig = async () => {
                try {
                    const res = await fetch(`${import.meta.env.VITE_API_BASE || 'http://localhost:8000'}/auth/config`);
                    if (res.ok) {
                        const data = await res.json();
                        setAppConfig(data);
                    }
                } catch (e) {
                    console.error('Failed to fetch config:', e);
                }
            };
            fetchConfig();
        }
    }, [isPending]);

    const scrollToBottom = () => messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    useEffect(() => { scrollToBottom(); }, [messages, streamingTurn, pendingInterrupt]);

    // Restore the last viewed chat on page refresh / tab reopen.
    // loadHistory already calls /chat/{id}/pending, so any in-progress
    // interrupt is restored automatically as part of this.
    useEffect(() => {
        const saved = localStorage.getItem('lastActiveHistory');
        if (saved) {
            loadHistory(Number(saved));
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // Fetch models on mount
    useEffect(() => {
        const fetchModels = async () => {
            try {
                const res = await apiFetch('/models/');
                if (res.ok) {
                    const data = await res.json();
                    setModels(data.models || []);
                    setDefaultModel(data.default_model || '');
                    setSelectedModel(data.default_model || '');
                    setUserQuota({
                        used: data.quota_used,
                        total: data.quota_total,
                        remaining: data.quota_remaining,
                    });
                }
            } catch (e) {
                console.error('Failed to fetch models:', e);
            }
        };
        fetchModels();
    }, [apiFetch]);

    useEffect(() => { 
        setHistoryOffset(0);
        setHistories([]);
        setHasMoreHistory(true);
        fetchHistories(0, true); 
        if (viewMode === 'active') {
            fetchPinnedChats();
        }
        /* eslint-disable-next-line */ 
    }, [viewMode]);

    const fetchPinnedChats = async () => {
        try {
            const res = await apiFetch('/history/pinned');
            if (res.ok) {
                const data = await res.json();
                // Ensure we always get an array
                const items = Array.isArray(data) ? data : (Array.isArray(data?.items) ? data.items : []);
                setPinnedChats(items);
            }
        } catch (e) {
            console.error('Failed to fetch pinned chats:', e);
            setPinnedChats([]);
        }
    };

    const fetchHistories = async (offset = 0, reset = false) => {
        if (loadingMore && !reset) return;
        setLoadingMore(true);
        try {
            const endpoint = viewMode === 'archived' 
                ? `/history/archived?offset=${offset}&limit=20` 
                : `/history/?offset=${offset}&limit=20`;
            const res = await apiFetch(endpoint);
            if (res.ok) {
                const data = await res.json();
                const items = data.items || data;
                const hasMore = data.has_more ?? items.length >= 20;
                
                if (reset) {
                    setHistories(items);
                } else {
                    setHistories(prev => [...prev, ...items]);
                }
                setHasMoreHistory(hasMore);
                setHistoryOffset(offset + items.length);
                
                if (activeHistory && !items.find(h => h.id === activeHistory)) {
                    // Check if it's in pinned
                    const inPinned = Array.isArray(pinnedChats) && pinnedChats.find(h => h.id === activeHistory);
                    if (!inPinned && reset) {
                        setActiveHistory(null);
                        setMessages([]);
                        localStorage.removeItem('lastActiveHistory');
                    }
                }
            }
        } catch (e) {
            console.error(e);
        } finally {
            setLoadingMore(false);
        }
    };

    // Infinite scroll for history list
    const handleHistoryScroll = useCallback(() => {
        const container = historyListRef.current;
        if (!container || loadingMore || !hasMoreHistory) return;
        
        const { scrollTop, scrollHeight, clientHeight } = container;
        if (scrollHeight - scrollTop - clientHeight < 100) {
            fetchHistories(historyOffset);
        }
    }, [loadingMore, hasMoreHistory, historyOffset]);

    useEffect(() => {
        const container = historyListRef.current;
        if (container) {
            container.addEventListener('scroll', handleHistoryScroll);
            return () => container.removeEventListener('scroll', handleHistoryScroll);
        }
    }, [handleHistoryScroll]);

    const startNewChat = () => {
        stopRestoredStreamPolling();
        if (viewMode === 'archived') setViewMode('active');
        setPendingInterrupt(null);
        setActiveHistory(null);
        setMessages([{ role: 'assistant', text: 'How can I help you build today?' }]);
        setMessageOffset(0);
        setHasMoreMessages(false);
        localStorage.removeItem('lastActiveHistory');
    };

    const restoreStreamingState = useCallback(async (id) => {
        try {
            const res = await apiFetch(`/chat/${id}/stream-state`);
            if (!res.ok) return false;

            const data = await res.json();
            if (!data.streaming) {
                if (streamHistoryId === id) {
                    setStreamingTurn(null);
                    setStreamHistoryId(null);
                    setLoading(false);
                }
                return false;
            }

            setStreamHistoryId(id);
            setLoading(true);
            setStreamingTurn({
                blocks: data.blocks || [],
                finalText: data.text || '',
                meta: null,
                error: null,
                interrupt: null,
                files: data.attachments || [],
                model: data.model_name || null,
                tokens: {
                    input: data.input_tokens || 0,
                    output: data.output_tokens || 0,
                    reasoning: data.reasoning_tokens || 0,
                    total: data.total_tokens || 0,
                },
            });

            stopRestoredStreamPolling();
            streamRestoreTimerRef.current = setTimeout(async () => {
                streamRestoreTimerRef.current = null;
                const stillStreaming = await restoreStreamingState(id);
                if (!stillStreaming && activeHistoryRef.current === id) {
                    try {
                        const historyRes = await apiFetch(`/history/${id}?message_limit=50`);
                        if (historyRes.ok) {
                            const historyData = await historyRes.json();
                            const refreshedMessages = historyData.messages?.items || historyData.messages || [];
                            setMessages(
                                refreshedMessages.length > 0
                                    ? refreshedMessages
                                    : [{ role: 'assistant', text: 'How can I help you build today?' }]
                            );
                            setMessageOffset(refreshedMessages.length);
                            setHasMoreMessages(historyData.messages?.has_more || false);

                            const pendingRes = await apiFetch(`/chat/${id}/pending`);
                            if (pendingRes.ok) {
                                const pendingData = await pendingRes.json();
                                setPendingInterrupt(
                                    pendingData.interrupted ? { historyId: id, data: pendingData } : null
                                );
                            }
                        }
                    } catch {
                        /* ignore restore refresh failures */
                    }
                }
            }, 900);

            return true;
        } catch {
            return false;
        }
    }, [apiFetch, stopRestoredStreamPolling, streamHistoryId]);

    const loadHistory = async (id) => {
        try {
            const res = await apiFetch(`/history/${id}?message_limit=50`);
            if (res.ok) {
                const data = await res.json();
                setActiveHistory(id);
                // Persist so the page can restore this chat on refresh / reopen.
                localStorage.setItem('lastActiveHistory', String(id));
                const msgs = data.messages?.items || data.messages || [];
                setMessages(
                    msgs.length > 0
                        ? msgs
                        : [{ role: 'assistant', text: 'How can I help you build today?' }]
                );
                setMessageOffset(msgs.length);
                setHasMoreMessages(data.messages?.has_more || false);

                const restoredStream = await restoreStreamingState(id);

                // Restore a pending interrupt if this conversation's agent run is
                // paused awaiting a human decision (survives reloads / tab reopens /
                // chat switches). Skip if it's the conversation currently streaming.
                setPendingInterrupt(null);
                if (!restoredStream) {
                    try {
                        const pres = await apiFetch(`/chat/${id}/pending`);
                        if (pres.ok) {
                            const pdata = await pres.json();
                            if (pdata.interrupted) {
                                setPendingInterrupt({ historyId: id, data: pdata });
                            }
                        }
                    } catch {
                        /* ignore — interrupt restore is best-effort */
                    }
                }
            } else {
                // History no longer exists — drop the stored reference so we
                // don't keep trying to restore a deleted conversation.
                if (localStorage.getItem('lastActiveHistory') === String(id)) {
                    localStorage.removeItem('lastActiveHistory');
                }
            }
        } catch (e) {
            console.error(e);
        }
    };

    // Load more messages (older) when scrolling up.
    // Memoized so the scroll handler captures a stable reference.
    const loadMoreMessages = useCallback(async () => {
        if (!activeHistory || loadingMoreMessages || !hasMoreMessages) return;
        const container = messagesContainerRef.current;
        // Save scroll height before prepending so we can restore position
        // after React re-renders — otherwise the view jumps to the top.
        const prevScrollHeight = container?.scrollHeight ?? 0;
        setLoadingMoreMessages(true);
        try {
            const res = await apiFetch(`/history/${activeHistory}/messages?offset=${messageOffset}&limit=50`);
            if (res.ok) {
                const data = await res.json();
                const olderMsgs = data.items || [];
                setMessages(prev => [...olderMsgs, ...prev]);
                setMessageOffset(prev => prev + olderMsgs.length);
                setHasMoreMessages(data.has_more || false);
                // Restore scroll after the DOM updates from the prepend
                requestAnimationFrame(() => {
                    if (container) {
                        container.scrollTop = container.scrollHeight - prevScrollHeight;
                    }
                });
            }
        } catch (e) {
            console.error(e);
        } finally {
            setLoadingMoreMessages(false);
        }
    }, [activeHistory, loadingMoreMessages, hasMoreMessages, messageOffset, apiFetch]);

    const handleMessagesScroll = useCallback(() => {
        const container = messagesContainerRef.current;
        if (!container || loadingMoreMessages || !hasMoreMessages) return;
        if (container.scrollTop < 100) {
            loadMoreMessages();
        }
    }, [loadingMoreMessages, hasMoreMessages, loadMoreMessages]);

    useEffect(() => {
        const container = messagesContainerRef.current;
        if (container) {
            container.addEventListener('scroll', handleMessagesScroll);
            return () => container.removeEventListener('scroll', handleMessagesScroll);
        }
    }, [handleMessagesScroll]);

    const deleteHistory = async (e, id) => {
        e.stopPropagation();
        setOpenMenuId(null);
        try {
            await apiFetch(`/history/${id}`, { method: 'DELETE' });
            setHistories(histories.filter(h => h.id !== id));
            setPinnedChats(prev => Array.isArray(prev) ? prev.filter(h => h.id !== id) : []);
            if (activeHistory === id) {
                setActiveHistory(null);
                setMessages([]);
                localStorage.removeItem('lastActiveHistory');
            }
        } catch (err) {
            console.error(err);
        }
    };

    const renameHistory = async (e, id, currentTitle) => {
        e.stopPropagation();
        setOpenMenuId(null);
        const newTitle = prompt('Enter new name:', currentTitle);
        if (!newTitle) return;
        try {
            await apiFetch(`/history/${id}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ title: newTitle }),
            });
            fetchHistories(0, true);
            fetchPinnedChats();
        } catch (err) {
            console.error(err);
        }
    };

    const processArchive = async (e, id, action) => {
        e.stopPropagation();
        setOpenMenuId(null);
        try {
            await apiFetch(`/history/${id}/${action}`, { method: 'PATCH' });
            setHistories(histories.filter(h => h.id !== id));
            setPinnedChats(prev => Array.isArray(prev) ? prev.filter(h => h.id !== id) : []);
            if (activeHistory === id) {
                setActiveHistory(null);
                setMessages([]);
                localStorage.removeItem('lastActiveHistory');
            }
        } catch (err) {
            console.error(err);
        }
    };

    const togglePin = async (e, id, isPinned) => {
        e.stopPropagation();
        setOpenMenuId(null);
        try {
            const action = isPinned ? 'unpin' : 'pin';
            const res = await apiFetch(`/history/${id}/${action}`, { method: 'PATCH' });
            if (res.ok) {
                fetchHistories(0, true);
                fetchPinnedChats();
            } else {
                const data = await res.json();
                alert(data.detail || 'Failed to pin/unpin');
            }
        } catch (err) {
            console.error(err);
        }
    };

    const sendMessage = async (e) => {
        e?.preventDefault();
        const text = input.trim();
        if (!text || loading || viewMode === 'archived') return;

        setMessages(prev => [...prev, { role: 'user', text }]);
        setInput('');
        if (inputRef.current) inputRef.current.style.height = 'auto';
        await runStream({ message: text, model_id: selectedModel || undefined });
    };

    // Enter sends; Shift+Enter inserts a newline for multiline prompts.
    const handleInputKeyDown = (e) => {
        if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            sendMessage();
        }
    };

    // Auto-grow the textarea up to a cap as the user types multiple lines.
    const handleInputChange = (e) => {
        setInput(e.target.value);
        const el = e.target;
        el.style.height = 'auto';
        el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
    };

    // Close model dropdown when clicking outside
    useEffect(() => {
        const handleClickOutside = (e) => {
            if (modelDropdownRef.current && !modelDropdownRef.current.contains(e.target)) {
                setShowModelDropdown(false);
            }
        };
        document.addEventListener('mousedown', handleClickOutside);
        return () => document.removeEventListener('mousedown', handleClickOutside);
    }, []);

    // Close actions menu when clicking outside
    useEffect(() => {
        const handleClickOutside = (e) => {
            if (openMenuId !== null && !e.target.closest('.actions-menu-container')) {
                setOpenMenuId(null);
            }
        };
        document.addEventListener('click', handleClickOutside);
        return () => document.removeEventListener('click', handleClickOutside);
    }, [openMenuId]);

    /**
     * Stream a chat turn (new message or HITL resume) and fold events into a
     * live turn, finalizing it into the message list when done. Returns true
     * on success, false on stream error (so callers can restore state).
     */
    const runStream = async (extraBody) => {
        const startedWithHistory = activeHistory;
        stopRestoredStreamPolling();
        setLoading(true);
        setPendingInterrupt(null);
        setStreamingTurn({ blocks: [], finalText: '', meta: null, error: null, interrupt: null, tokens: null, model: null });
        setStreamHistoryId(startedWithHistory);

        const controller = new AbortController();
        abortRef.current = controller;

        let streamId = startedWithHistory;
        let newHistoryId = null;
        let tokenData = null;
        let streamFailed = false;
        try {
            newHistoryId = await streamChat(
                apiFetch,
                { history_id: startedWithHistory, ...extraBody },
                {
                    signal: controller.signal,
                    onEvent: (evt) => {
                        setStreamingTurn(prev => (prev ? reduceEvent(prev, evt) : prev));
                        if (evt.event === 'token_usage') {
                            tokenData = { model: evt.data?.model, tokens: evt.data?.tokens };
                        }
                        if (evt.event === 'agent_message' && !evt.data?.is_subagent && evt.data?.tokens) {
                            tokenData = {
                                model: evt.data?.model || tokenData?.model,
                                tokens: evt.data?.tokens,
                            };
                        }
                    },
                    onHistoryId: (hid) => {
                        const num = Number(hid);
                        streamId = num;
                        setStreamHistoryId(num);
                        if (!startedWithHistory && activeHistoryRef.current === null) {
                            setActiveHistory(num);
                        }
                        // Only refetch history list for brand-new chats — existing
                        // chats refetch after the stream finishes to avoid flicker.
                        if (!startedWithHistory) {
                            fetchHistories(0, true);
                            fetchPinnedChats();
                        }
                    },
                }
            );
        } catch (err) {
            if (err.name !== 'AbortError') {
                streamFailed = true;
                setStreamingTurn(prev => ({ ...(prev || { blocks: [], finalText: '' }), error: err.message }));
            }
        } finally {
            stopRestoredStreamPolling();
            const resolvedHistory = streamId || (newHistoryId ? Number(newHistoryId) : null);
            const stillViewing = activeHistoryRef.current === resolvedHistory;

            setStreamingTurn(prev => {
                if (prev) {
                    const { timeline, final } = splitFinal(prev.blocks, prev.finalText);
                    if (stillViewing && (timeline.length || final || prev.error)) {
                        setMessages(msgs => [
                            ...msgs,
                            {
                                role: 'assistant',
                                text: final,
                                blocks: timeline,
                                attachments: prev.files || null,
                                historyId: resolvedHistory,
                                error: prev.error,
                                model_name: prev.model || tokenData?.model || null,
                                input_tokens: prev.tokens?.input || tokenData?.tokens?.input || 0,
                                output_tokens: prev.tokens?.output || tokenData?.tokens?.output || 0,
                                reasoning_tokens: prev.tokens?.reasoning || tokenData?.tokens?.reasoning || 0,
                                total_tokens: prev.tokens?.total || tokenData?.tokens?.total || 0,
                            },
                        ]);
                    }
                    if (prev.interrupt) {
                        setPendingInterrupt({ historyId: resolvedHistory, data: prev.interrupt });
                    }
                }
                return null;
            });

            setLoading(false);
            setStreamHistoryId(null);
            abortRef.current = null;
            fetchHistories(0, true);
            fetchPinnedChats();

            try {
                const qres = await apiFetch('/models/quota');
                if (qres.ok) {
                    const qdata = await qres.json();
                    setUserQuota({
                        used: qdata.tokens_used_this_month,
                        total: qdata.token_quota,
                        remaining: qdata.quota_remaining,
                    });
                }
            } catch { /* ignore */ }
        }
        return !streamFailed;
    };

    /** Resume a paused (interrupted) run with the user's decision.
     *  Preserves the interrupt state so the user can retry if the stream fails. */
    const resolveInterrupt = async (decision) => {
        if (!pendingInterrupt || loading) return;
        const savedInterrupt = pendingInterrupt;
        const ok = await runStream({
            interrupt_action: typeof decision === 'string' ? { type: decision } : decision,
            model_id: selectedModel || undefined,
        });
        if (!ok) {
            // Stream failed before connecting — restore so the user can retry.
            setPendingInterrupt(savedInterrupt);
        }
    };

    const stopStreaming = () => {
        abortRef.current?.abort();
    };

    useEffect(() => {
        const handleClickOutside = (event) => {
            if (isProfileOpen && !event.target.closest('.user-profile')) setIsProfileOpen(false);
        };
        document.addEventListener('click', handleClickOutside);
        return () => document.removeEventListener('click', handleClickOutside);
    }, [isProfileOpen]);

    useEffect(() => () => stopRestoredStreamPolling(), [stopRestoredStreamPolling]);

    const activeTitle = histories.find(h => h.id === activeHistory)?.title 
        || (Array.isArray(pinnedChats) ? pinnedChats.find(h => h.id === activeHistory)?.title : undefined);

    // A conversation is "busy" when it is actively streaming or waiting for a
    // human approval decision — shown as an animated indicator in the sidebar.
    const isBusy = (id) =>
        (loading && streamHistoryId === id) ||
        (pendingInterrupt && pendingInterrupt.historyId === id);

    // Only show the live stream / approval prompt on the conversation they
    // belong to (so switching chats mid-stream doesn't leak into another page).
    const streamVisible = streamingTurn && streamHistoryId === activeHistory;
    const interruptVisible = pendingInterrupt && pendingInterrupt.historyId === activeHistory;

    // Format token count for display
    const formatTokens = (count) => {
        if (count >= 1000000) return `${(count / 1000000).toFixed(1)}M`;
        if (count >= 1000) return `${(count / 1000).toFixed(1)}K`;
        return count.toString();
    };

    // Get selected model info
    const selectedModelInfo = models.find(m => m.id === selectedModel);

    // Render a history item with actions menu
    const renderHistoryItem = (h, isPinned = false) => (
        <div
            key={h.id}
            className={`history-item ${activeHistory === h.id ? 'active' : ''} ${isPinned ? 'pinned' : ''}`}
            onClick={() => loadHistory(h.id)}
            title={sidebarCollapsed ? h.title : ''}
        >
            <div className="history-item-content">
                {isPinned && <Pin size={12} className="pin-indicator" />}
                <MessageSquare size={16} />
                <span className="history-title">{h.title}</span>
                {isBusy(h.id) && (
                    <span className="history-busy" title="Working…">
                        <span /><span /><span />
                    </span>
                )}
            </div>
            <div className="actions-menu-container">
                <button 
                    className="actions-menu-trigger"
                    onClick={(e) => {
                        e.stopPropagation();
                        setOpenMenuId(openMenuId === h.id ? null : h.id);
                    }}
                    title="Actions"
                >
                    <MoreVertical size={16} />
                </button>
                {openMenuId === h.id && (
                    <div className="actions-menu animate-fade-in">
                        <button onClick={(e) => renameHistory(e, h.id, h.title)}>
                            <Edit2 size={14} /> Rename
                        </button>
                        {viewMode === 'active' && (
                            <>
                                <button onClick={(e) => togglePin(e, h.id, h.is_pinned || isPinned)}>
                                    {h.is_pinned || isPinned ? (
                                        <><PinOff size={14} /> Unpin</>
                                    ) : (
                                        <><Pin size={14} /> Pin</>
                                    )}
                                </button>
                                <button onClick={(e) => processArchive(e, h.id, 'archive')}>
                                    <Archive size={14} /> Archive
                                </button>
                            </>
                        )}
                        {viewMode === 'archived' && (
                            <button onClick={(e) => processArchive(e, h.id, 'unarchive')}>
                                <RefreshCw size={14} /> Unarchive
                            </button>
                        )}
                        <button className="danger" onClick={(e) => deleteHistory(e, h.id)}>
                            <Trash2 size={14} /> Delete
                        </button>
                    </div>
                )}
            </div>
        </div>
    );

    // Show pending approval message for pending users
    if (userInfo.role === 'pending') {
        return (
            <div className="pending-approval-container">
                <div className="pending-approval-card glass-panel">
                    <div className="pending-icon">
                        <Bot size={48} color="var(--accent-color)" />
                    </div>
                    <h2>Registration Request Pending</h2>
                    <p>Your account registration has been submitted and is awaiting admin approval.</p>
                    <div className="pending-info-box">
                        <strong>What happens next?</strong>
                        <ul>
                            <li>An administrator will review your request</li>
                            <li>You'll gain access once approved</li>
                            {appConfig.pending_user_expire_days > 0 && (
                                <li>Requests not approved within {appConfig.pending_user_expire_days} days will expire automatically</li>
                            )}
                        </ul>
                    </div>
                    <button className="btn-secondary" onClick={logout}>
                        <LogOut size={16} /> Sign Out
                    </button>
                </div>
            </div>
        );
    }

    return (
        <div className={`dashboard-layout${artifact ? ' artifact-open' : ''}${sidebarCollapsed ? ' sidebar-collapsed' : ''}`}>
            <button
                className="theme-toggle"
                onClick={() => setIsDarkMode(!isDarkMode)}
                title={isDarkMode ? 'Switch to Light Mode' : 'Switch to Dark Mode'}
            >
                {isDarkMode ? <Sun size={20} /> : <Moon size={20} />}
            </button>

            <aside className={`sidebar${sidebarCollapsed ? ' collapsed' : ''}`}>
                <div className="sidebar-header">
                    <div className="logo-container" style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                        <Bot size={24} color="var(--accent-color)" />
                        {!sidebarCollapsed && <h2 style={{ fontSize: '18px' }}>My Agent</h2>}
                    </div>
                </div>

                {viewMode === 'active' ? (
                    <button className="btn-primary new-chat-btn" onClick={startNewChat} title="New Agent Task">
                        <Plus size={18} /> {!sidebarCollapsed && 'New Agent Task'}
                    </button>
                ) : (
                    <button
                        className="btn-primary new-chat-btn"
                        onClick={() => setViewMode('active')}
                        style={{ background: 'var(--bg-panel)', border: '1px solid var(--border-glass)' }}
                        title="Back to Active"
                    >
                        <ArrowLeft size={18} /> {!sidebarCollapsed && 'Back to Active'}
                    </button>
                )}

                <div className="history-list" ref={historyListRef}>
                    {/* Pinned chats section */}
                    {viewMode === 'active' && Array.isArray(pinnedChats) && pinnedChats.length > 0 && (
                        <div className="pinned-section">
                            <div className="section-label">
                                <Pin size={12} /> Pinned
                            </div>
                            {pinnedChats.map(h => renderHistoryItem(h, true))}
                        </div>
                    )}
                    
                    {/* Regular chats */}
                    {viewMode === 'active' && Array.isArray(pinnedChats) && pinnedChats.length > 0 && histories.length > 0 && (
                        <div className="section-label">Recent</div>
                    )}
                    
                    {histories.length === 0 && (!Array.isArray(pinnedChats) || pinnedChats.length === 0) && !loadingMore && !sidebarCollapsed && (
                        <div style={{ textAlign: 'center', padding: '20px', color: 'var(--text-muted)', fontSize: '14px' }}>
                            No {viewMode} chats found
                        </div>
                    )}
                    {histories.map(h => renderHistoryItem(h, false))}
                    
                    {/* Loading indicator for infinite scroll */}
                    {loadingMore && !sidebarCollapsed && (
                        <div style={{ textAlign: 'center', padding: '12px', color: 'var(--text-muted)' }}>
                            Loading more...
                        </div>
                    )}
                </div>

                {/* Collapse toggle button */}
                <button 
                    className="sidebar-toggle"
                    onClick={() => setSidebarCollapsed(!sidebarCollapsed)}
                    title={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
                >
                    {sidebarCollapsed ? (
                        <><ChevronsRight size={16} /></>
                    ) : (
                        <><ChevronsLeft size={16} /> <span>Collapse</span></>
                    )}
                </button>

                <div className="user-profile" onClick={() => setIsProfileOpen(!isProfileOpen)} title={sidebarCollapsed ? userInfo.username : ''}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                        <div className="avatar"><UserIcon size={20} color="white" /></div>
                        {!sidebarCollapsed && (
                            <div>
                                <span style={{ fontWeight: 500, fontSize: '14px' }}>{userInfo.username}</span>
                                {isAdmin && <span className="admin-badge">Admin</span>}
                            </div>
                        )}
                    </div>
                    {!sidebarCollapsed && <ChevronDown size={18} color="var(--text-muted)" style={{ transform: isProfileOpen ? 'rotate(180deg)' : 'none', transition: 'transform 0.2s' }} />}

                    {isProfileOpen && (
                        <div className="profile-dropdown animate-fade-in" onClick={(e) => e.stopPropagation()}>
                            {/* Quota display */}
                            {userQuota && (
                                <div className="quota-display">
                                    <div className="quota-header">
                                        <Zap size={14} />
                                        {userQuota.total === -1 ? (
                                            <span>Unlimited (used: {formatTokens(userQuota.used)})</span>
                                        ) : (
                                            <>
                                                <span>{formatTokens(userQuota.used)} / {formatTokens(userQuota.total)}</span>
                                                <span className="quota-percent">({Math.round((userQuota.used / userQuota.total) * 100)}%)</span>
                                            </>
                                        )}
                                    </div>
                                    {userQuota.total !== -1 && (
                                        <div className="quota-bar-small">
                                            <div 
                                                className={`quota-fill-small ${(userQuota.used / userQuota.total) >= 0.9 ? 'warning' : ''}`}
                                                style={{ width: `${Math.min(100, (userQuota.used / userQuota.total) * 100)}%` }}
                                            />
                                        </div>
                                    )}
                                </div>
                            )}
                            {isAdmin && (
                                <a href="/admin" className="dropdown-item" onClick={(e) => { e.preventDefault(); window.location.href = '/admin'; }}>
                                    <Settings size={16} /> Admin Panel
                                </a>
                            )}
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
                        {activeTitle || 'Select or start a new task'}
                    </h2>
                </header>

                <div className="chat-container" ref={messagesContainerRef}>
                    {/* Load more messages indicator */}
                    {hasMoreMessages && (
                        <div style={{ textAlign: 'center', padding: '12px' }}>
                            <button 
                                className="load-more-btn"
                                onClick={loadMoreMessages}
                                disabled={loadingMoreMessages}
                            >
                                {loadingMoreMessages ? 'Loading...' : 'Load earlier messages'}
                            </button>
                        </div>
                    )}
                    
                    {messages.map((msg, idx) => {
                        if (msg.role === 'user') {
                            return (
                                <div key={idx} className="message-wrapper user">
                                    <div className="message-bubble">
                                        <pre style={{ fontFamily: 'inherit', whiteSpace: 'pre-wrap', margin: 0 }}>{msg.text}</pre>
                                    </div>
                                </div>
                            );
                        }
                        if (msg.role === 'system') {
                            return <div key={idx} className="system-note">{msg.text}</div>;
                        }
                        // assistant
                        const hasBlocks = (msg.blocks && msg.blocks.length) || (msg.attachments && msg.attachments.length);
                        const hasTokenInfo = msg.model_name || msg.total_tokens > 0;
                        
                        return (
                            <div key={idx} className="message-wrapper assistant">
                                {hasBlocks ? (
                                    <AgentTurn
                                        timeline={msg.blocks || []}
                                        finalText={msg.text}
                                        error={msg.error}
                                        files={msg.attachments || null}
                                        historyId={msg.historyId || activeHistory}
                                    />
                                ) : (
                                    <div className="message-bubble">
                                        <pre style={{ fontFamily: 'inherit', whiteSpace: 'pre-wrap', margin: 0 }}>{msg.text}</pre>
                                    </div>
                                )}
                                {/* Token usage display */}
                                {hasTokenInfo && (
                                    <div className="token-info">
                                        {msg.model_name && <span className="model-badge">{msg.model_name}</span>}
                                        {(msg.total_tokens > 0 || msg.input_tokens > 0) && (
                                            <span className="token-counts">
                                                <span title="Input tokens">↓{formatTokens(msg.input_tokens || 0)}</span>
                                                {msg.reasoning_tokens > 0 && (
                                                    <span title="Reasoning tokens">🧠{formatTokens(msg.reasoning_tokens)}</span>
                                                )}
                                                <span title="Output tokens">↑{formatTokens(msg.output_tokens || 0)}</span>
                                                <span title="Total tokens">Σ{formatTokens(msg.total_tokens || (msg.input_tokens || 0) + (msg.output_tokens || 0))}</span>
                                            </span>
                                        )}
                                    </div>
                                )}
                            </div>
                        );
                    })}

                    {streamVisible && (() => {
                        const { timeline, final } = splitFinal(streamingTurn.blocks, streamingTurn.finalText);
                        return (
                            <div className="message-wrapper assistant">
                                <AgentTurn
                                    timeline={timeline}
                                    finalText={final}
                                    streaming
                                    error={streamingTurn.error}
                                    files={streamingTurn.files || null}
                                    historyId={streamHistoryId}
                                />
                                {/* Show token info if available during/after stream */}
                                {(streamingTurn.model || streamingTurn.tokens) && (
                                    <div className="token-info">
                                        {streamingTurn.model && <span className="model-badge">{streamingTurn.model}</span>}
                                        {streamingTurn.tokens && (streamingTurn.tokens.total > 0 || streamingTurn.tokens.input > 0) && (
                                            <span className="token-counts">
                                                <span title="Input tokens">↓{formatTokens(streamingTurn.tokens.input || 0)}</span>
                                                {streamingTurn.tokens.reasoning > 0 && (
                                                    <span title="Reasoning tokens">🧠{formatTokens(streamingTurn.tokens.reasoning)}</span>
                                                )}
                                                <span title="Output tokens">↑{formatTokens(streamingTurn.tokens.output || 0)}</span>
                                                <span title="Total tokens">Σ{formatTokens(streamingTurn.tokens.total || 0)}</span>
                                            </span>
                                        )}
                                    </div>
                                )}
                            </div>
                        );
                    })()}

                    {interruptVisible && (
                        <div className="message-wrapper assistant">
                            <InterruptCard
                                key={`${pendingInterrupt.historyId}-${JSON.stringify(pendingInterrupt.data?.payload ?? pendingInterrupt.data)}`}
                                interrupt={pendingInterrupt.data}
                                busy={loading}
                                onDecide={resolveInterrupt}
                            />
                        </div>
                    )}

                    <div ref={messagesEndRef} />
                </div>

                <div className="input-area">
                    <form className="input-form" onSubmit={sendMessage}>
                        {/* Model selector dropdown */}
                        <div className="model-selector" ref={modelDropdownRef}>
                            <button
                                type="button"
                                className="model-selector-btn"
                                onClick={() => setShowModelDropdown(!showModelDropdown)}
                                disabled={loading}
                                title={selectedModelInfo?.name || 'Select model'}
                            >
                                <Bot size={16} />
                                <span className="model-name">{selectedModelInfo?.name || selectedModel || 'Model'}</span>
                                <ChevronDown size={14} />
                            </button>
                            {showModelDropdown && (
                                <div className="model-dropdown animate-fade-in">
                                    {models.map(m => (
                                        <button
                                            key={m.id}
                                            type="button"
                                            className={`model-option ${selectedModel === m.id ? 'selected' : ''}`}
                                            onClick={() => {
                                                setSelectedModel(m.id);
                                                setShowModelDropdown(false);
                                            }}
                                        >
                                            <div className="model-option-main">
                                                <span className="model-option-name">{m.name}</span>
                                                {m.is_free && <span className="free-badge">Free</span>}
                                                {m.id === defaultModel && <span className="default-badge">Default</span>}
                                            </div>
                                            <span className="model-option-desc">{m.description}</span>
                                        </button>
                                    ))}
                                </div>
                            )}
                        </div>
                        
                        <textarea
                            ref={inputRef}
                            className="chat-input"
                            rows={1}
                            value={input}
                            onChange={handleInputChange}
                            onKeyDown={handleInputKeyDown}
                            placeholder={
                                viewMode === 'archived'
                                    ? 'Cannot send messages in archived chats'
                                    : interruptVisible
                                        ? 'Approve or reject the pending action above…'
                                        : loading
                                            ? 'Agent is working…'
                                            : 'Command your agent…  (Shift+Enter for new line)'
                            }
                            disabled={loading || viewMode === 'archived' || !!interruptVisible}
                        />
                        {loading ? (
                            <button type="button" className="send-btn stop" onClick={stopStreaming} title="Stop">
                                <Square size={16} />
                            </button>
                        ) : (
                            <button
                                type="submit"
                                className="send-btn"
                                disabled={!input.trim() || viewMode === 'archived' || !!interruptVisible}
                                title="Send message"
                            >
                                <Send size={18} />
                            </button>
                        )}
                    </form>
                </div>
            </main>

            <ArtifactPanel />
        </div>
    );
}

function Dashboard() {
    return (
        <ArtifactProvider>
            <DashboardInner />
        </ArtifactProvider>
    );
}

export default Dashboard;
