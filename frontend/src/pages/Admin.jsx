import { useEffect, useState, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../components/AuthContext';
import {
    Users, BarChart2, Settings, ArrowLeft, Check, X,
    UserPlus, Edit2, Trash2, ChevronDown, RefreshCw,
    Zap, MessageSquare, Bot, Plus, Save
} from 'lucide-react';

const TABS = {
    users: 'users',
    stats: 'stats',
    models: 'models',
};

function Admin() {
    const { token, apiFetch } = useAuth();
    const navigate = useNavigate();
    
    const [activeTab, setActiveTab] = useState(TABS.users);
    const [loading, setLoading] = useState(false);
    
    // Users state
    const [users, setUsers] = useState([]);
    const [pendingUsers, setPendingUsers] = useState([]);
    const [usersOffset, setUsersOffset] = useState(0);
    const [hasMoreUsers, setHasMoreUsers] = useState(true);
    const [userFilter, setUserFilter] = useState('all'); // all | pending | approved
    const [selectedUser, setSelectedUser] = useState(null);
    const [editingQuota, setEditingQuota] = useState(null);
    const [quotaAmount, setQuotaAmount] = useState('');
    
    // Stats state
    const [overallStats, setOverallStats] = useState(null);
    const [userStats, setUserStats] = useState([]);
    const [statsOffset, setStatsOffset] = useState(0);
    const [hasMoreStats, setHasMoreStats] = useState(true);
    const [sortBy, setSortBy] = useState('total_tokens');
    
    // Models state
    const [modelsConfig, setModelsConfig] = useState(null);
    const [editingModel, setEditingModel] = useState(null);
    const [newModel, setNewModel] = useState(null);

    // Check admin access
    useEffect(() => {
        if (!token) {
            navigate('/login');
            return;
        }
        try {
            const payload = JSON.parse(atob(token.split('.')[1]));
            if (payload.role !== 'admin') {
                navigate('/');
            }
        } catch {
            navigate('/login');
        }
    }, [token, navigate]);

    // Fetch data based on active tab
    useEffect(() => {
        if (activeTab === TABS.users) {
            fetchUsers(0, true);
            fetchPendingUsers();
        } else if (activeTab === TABS.stats) {
            fetchOverallStats();
            fetchUserStats(0, true);
        } else if (activeTab === TABS.models) {
            fetchModelsConfig();
        }
    }, [activeTab]);

    // ═══════════════════════════════════════════════════════════════════════════════
    // Users Management
    // ═══════════════════════════════════════════════════════════════════════════════
    
    const fetchUsers = async (offset = 0, reset = false) => {
        setLoading(true);
        try {
            const params = new URLSearchParams({ offset, limit: 20 });
            if (userFilter === 'pending') params.append('role', 'pending');
            if (userFilter === 'approved') params.append('approved', 'true');
            
            const res = await apiFetch(`/admin/users?${params}`);
            if (res.ok) {
                const data = await res.json();
                if (reset) {
                    setUsers(data.items || []);
                } else {
                    setUsers(prev => [...prev, ...(data.items || [])]);
                }
                setHasMoreUsers(data.has_more || false);
                setUsersOffset(offset + (data.items?.length || 0));
            }
        } catch (e) {
            console.error('Failed to fetch users:', e);
        } finally {
            setLoading(false);
        }
    };

    const fetchPendingUsers = async () => {
        try {
            const res = await apiFetch('/admin/users/pending');
            if (res.ok) {
                const data = await res.json();
                setPendingUsers(data || []);
            }
        } catch (e) {
            console.error('Failed to fetch pending users:', e);
        }
    };

    const approveUser = async (userId) => {
        try {
            const res = await apiFetch(`/admin/users/${userId}/approve`, { method: 'POST' });
            if (res.ok) {
                fetchPendingUsers();
                fetchUsers(0, true);
            }
        } catch (e) {
            console.error('Failed to approve user:', e);
        }
    };

    const rejectUser = async (userId) => {
        if (!confirm('Are you sure you want to reject and delete this user?')) return;
        try {
            const res = await apiFetch(`/admin/users/${userId}/reject`, { method: 'POST' });
            if (res.ok) {
                fetchPendingUsers();
            }
        } catch (e) {
            console.error('Failed to reject user:', e);
        }
    };

    const updateUserQuota = async (userId, newQuota) => {
        try {
            const res = await apiFetch(`/admin/users/${userId}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ token_quota: parseInt(newQuota) }),
            });
            if (res.ok) {
                setEditingQuota(null);
                setQuotaAmount('');
                fetchUsers(0, true);
            }
        } catch (e) {
            console.error('Failed to update quota:', e);
        }
    };

    const increaseQuota = async (userId) => {
        const amount = prompt('Enter additional tokens to grant:', '10000');
        if (!amount || isNaN(parseInt(amount))) return;
        
        try {
            const res = await apiFetch(`/admin/users/${userId}/increase-quota`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ additional_tokens: parseInt(amount) }),
            });
            if (res.ok) {
                fetchUsers(0, true);
                alert('Quota increased successfully');
            }
        } catch (e) {
            console.error('Failed to increase quota:', e);
        }
    };

    const updateUserRole = async (userId, newRole) => {
        try {
            const res = await apiFetch(`/admin/users/${userId}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ role: newRole }),
            });
            if (res.ok) {
                fetchUsers(0, true);
            }
        } catch (e) {
            console.error('Failed to update role:', e);
        }
    };

    // ═══════════════════════════════════════════════════════════════════════════════
    // Statistics
    // ═══════════════════════════════════════════════════════════════════════════════
    
    const fetchOverallStats = async () => {
        try {
            const res = await apiFetch('/admin/stats/overview');
            if (res.ok) {
                const data = await res.json();
                setOverallStats(data);
            }
        } catch (e) {
            console.error('Failed to fetch overall stats:', e);
        }
    };

    const fetchUserStats = async (offset = 0, reset = false) => {
        setLoading(true);
        try {
            const params = new URLSearchParams({ offset, limit: 20, sort_by: sortBy });
            const res = await apiFetch(`/admin/stats/users?${params}`);
            if (res.ok) {
                const data = await res.json();
                if (reset) {
                    setUserStats(data.items || []);
                } else {
                    setUserStats(prev => [...prev, ...(data.items || [])]);
                }
                setHasMoreStats(data.has_more || false);
                setStatsOffset(offset + (data.items?.length || 0));
            }
        } catch (e) {
            console.error('Failed to fetch user stats:', e);
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        if (activeTab === TABS.stats) {
            fetchUserStats(0, true);
        }
    }, [sortBy]);

    // ═══════════════════════════════════════════════════════════════════════════════
    // Models Configuration
    // ═══════════════════════════════════════════════════════════════════════════════
    
    const fetchModelsConfig = async () => {
        setLoading(true);
        try {
            const res = await apiFetch('/admin/models');
            if (res.ok) {
                const data = await res.json();
                setModelsConfig(data);
            }
        } catch (e) {
            console.error('Failed to fetch models config:', e);
        } finally {
            setLoading(false);
        }
    };

    const saveModel = async (modelId, modelData) => {
        try {
            const isFree = modelsConfig?.tiers?.free?.includes(modelId) || false;
            const res = await apiFetch(`/admin/models/${modelId}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ...modelData, is_free: isFree }),
            });
            if (res.ok) {
                setEditingModel(null);
                setNewModel(null);
                fetchModelsConfig();
            }
        } catch (e) {
            console.error('Failed to save model:', e);
        }
    };

    const deleteModel = async (modelId) => {
        if (!confirm(`Are you sure you want to delete model "${modelId}"?`)) return;
        try {
            const res = await apiFetch(`/admin/models/${modelId}`, { method: 'DELETE' });
            if (res.ok) {
                fetchModelsConfig();
            }
        } catch (e) {
            console.error('Failed to delete model:', e);
        }
    };

    const setDefaultModel = async (modelId) => {
        try {
            const res = await apiFetch('/admin/models', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ default_model: modelId }),
            });
            if (res.ok) {
                fetchModelsConfig();
            }
        } catch (e) {
            console.error('Failed to set default model:', e);
        }
    };

    const toggleModelTier = async (modelId, makeFree) => {
        if (!modelsConfig) return;
        
        const newTiers = { ...modelsConfig.tiers };
        if (makeFree) {
            newTiers.free = [...(newTiers.free || []), modelId];
            newTiers.paid = (newTiers.paid || []).filter(id => id !== modelId);
        } else {
            newTiers.paid = [...(newTiers.paid || []), modelId];
            newTiers.free = (newTiers.free || []).filter(id => id !== modelId);
        }
        
        try {
            const res = await apiFetch('/admin/models', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ tiers: newTiers }),
            });
            if (res.ok) {
                fetchModelsConfig();
            }
        } catch (e) {
            console.error('Failed to update model tier:', e);
        }
    };

    const reloadModels = async () => {
        try {
            const res = await apiFetch('/admin/models/reload', { method: 'POST' });
            if (res.ok) {
                fetchModelsConfig();
                alert('Models configuration reloaded from disk');
            }
        } catch (e) {
            console.error('Failed to reload models:', e);
        }
    };

    // ═══════════════════════════════════════════════════════════════════════════════
    // Formatting helpers
    // ═══════════════════════════════════════════════════════════════════════════════
    
    const formatNumber = (num) => {
        if (num >= 1000000) return `${(num / 1000000).toFixed(1)}M`;
        if (num >= 1000) return `${(num / 1000).toFixed(1)}K`;
        return num.toString();
    };

    const formatDate = (dateStr) => {
        if (!dateStr) return '-';
        return new Date(dateStr).toLocaleDateString();
    };

    // ═══════════════════════════════════════════════════════════════════════════════
    // Render
    // ═══════════════════════════════════════════════════════════════════════════════

    return (
        <div className="admin-layout">
            {/* Sidebar Navigation */}
            <aside className="admin-sidebar">
                <div className="admin-header">
                    <button className="back-btn" onClick={() => navigate('/')}>
                        <ArrowLeft size={18} />
                    </button>
                    <h2>Admin Panel</h2>
                </div>
                
                <nav className="admin-nav">
                    <button
                        className={`nav-item ${activeTab === TABS.users ? 'active' : ''}`}
                        onClick={() => setActiveTab(TABS.users)}
                    >
                        <Users size={18} />
                        <span>Users</span>
                        {pendingUsers.length > 0 && (
                            <span className="badge">{pendingUsers.length}</span>
                        )}
                    </button>
                    <button
                        className={`nav-item ${activeTab === TABS.stats ? 'active' : ''}`}
                        onClick={() => setActiveTab(TABS.stats)}
                    >
                        <BarChart2 size={18} />
                        <span>Statistics</span>
                    </button>
                    <button
                        className={`nav-item ${activeTab === TABS.models ? 'active' : ''}`}
                        onClick={() => setActiveTab(TABS.models)}
                    >
                        <Settings size={18} />
                        <span>Models</span>
                    </button>
                </nav>
            </aside>

            {/* Main Content */}
            <main className="admin-content">
                {/* Users Tab */}
                {activeTab === TABS.users && (
                    <div className="admin-section">
                        <h1>User Management</h1>
                        
                        {/* Pending Users */}
                        {pendingUsers.length > 0 && (
                            <div className="pending-section">
                                <h3>Pending Approvals ({pendingUsers.length})</h3>
                                <div className="pending-list">
                                    {pendingUsers.map(user => (
                                        <div key={user.id} className="pending-card">
                                            <div className="pending-info">
                                                <strong>{user.username}</strong>
                                                <span className="pending-date">Registered: {formatDate(user.created_at)}</span>
                                            </div>
                                            <div className="pending-actions">
                                                <button className="btn-approve" onClick={() => approveUser(user.id)}>
                                                    <Check size={16} /> Approve
                                                </button>
                                                <button className="btn-reject" onClick={() => rejectUser(user.id)}>
                                                    <X size={16} /> Reject
                                                </button>
                                            </div>
                                        </div>
                                    ))}
                                </div>
                            </div>
                        )}
                        
                        {/* User Filter */}
                        <div className="filter-bar">
                            <select value={userFilter} onChange={(e) => { setUserFilter(e.target.value); fetchUsers(0, true); }}>
                                <option value="all">All Users</option>
                                <option value="approved">Approved Only</option>
                                <option value="pending">Pending Only</option>
                            </select>
                            <button className="btn-refresh" onClick={() => fetchUsers(0, true)}>
                                <RefreshCw size={16} />
                            </button>
                        </div>
                        
                        {/* Users Table */}
                        <div className="data-table">
                            <table>
                                <thead>
                                    <tr>
                                        <th>Username</th>
                                        <th>Role</th>
                                        <th>Status</th>
                                        <th>Quota</th>
                                        <th>Usage</th>
                                        <th>Chats</th>
                                        <th>Actions</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {users.map(user => (
                                        <tr key={user.id}>
                                            <td><strong>{user.username}</strong></td>
                                            <td>
                                                <select
                                                    value={user.role}
                                                    onChange={(e) => updateUserRole(user.id, e.target.value)}
                                                    className="role-select"
                                                >
                                                    <option value="pending">Pending</option>
                                                    <option value="user">User</option>
                                                    <option value="admin">Admin</option>
                                                </select>
                                            </td>
                                            <td>
                                                <span className={`status-badge ${user.is_approved ? 'approved' : 'pending'}`}>
                                                    {user.is_approved ? 'Approved' : 'Pending'}
                                                </span>
                                            </td>
                                            <td>
                                                {editingQuota === user.id ? (
                                                    <div className="quota-edit">
                                                        <input
                                                            type="number"
                                                            value={quotaAmount}
                                                            onChange={(e) => setQuotaAmount(e.target.value)}
                                                            placeholder={user.token_quota.toString()}
                                                        />
                                                        <button onClick={() => updateUserQuota(user.id, quotaAmount)}><Check size={14} /></button>
                                                        <button onClick={() => setEditingQuota(null)}><X size={14} /></button>
                                                    </div>
                                                ) : (
                                                    <span onClick={() => { setEditingQuota(user.id); setQuotaAmount(user.token_quota.toString()); }} style={{ cursor: 'pointer' }}>
                                                        {user.token_quota === -1 ? '∞' : formatNumber(user.token_quota)}
                                                    </span>
                                                )}
                                            </td>
                                            <td>
                                                {user.token_quota === -1 ? (
                                                    <span className="unlimited">∞</span>
                                                ) : (
                                                    <div className="quota-usage-cell">
                                                        <div className="quota-bar-wrapper">
                                                            <div 
                                                                className={`quota-fill ${(user.tokens_used_this_month / user.token_quota) >= 0.9 ? 'warning' : ''}`}
                                                                style={{ width: `${Math.min(100, (user.tokens_used_this_month / user.token_quota) * 100)}%` }}
                                                            />
                                                        </div>
                                                        <span className="quota-text">
                                                            {formatNumber(user.tokens_used_this_month)} / {formatNumber(user.token_quota)}
                                                            <span className="quota-percent">({Math.round((user.tokens_used_this_month / user.token_quota) * 100)}%)</span>
                                                        </span>
                                                    </div>
                                                )}
                                            </td>
                                            <td>{user.chat_count}</td>
                                            <td>
                                                <button className="btn-icon" title="Increase Quota" onClick={() => increaseQuota(user.id)}>
                                                    <Zap size={14} />
                                                </button>
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                        
                        {hasMoreUsers && (
                            <button className="btn-load-more" onClick={() => fetchUsers(usersOffset)} disabled={loading}>
                                {loading ? 'Loading...' : 'Load More'}
                            </button>
                        )}
                    </div>
                )}

                {/* Stats Tab */}
                {activeTab === TABS.stats && (
                    <div className="admin-section">
                        <h1>Statistics</h1>
                        
                        {/* Overview Cards */}
                        {overallStats && (
                            <div className="stats-grid">
                                <div className="stat-card">
                                    <Users size={24} />
                                    <div className="stat-value">{overallStats.total_users}</div>
                                    <div className="stat-label">Total Users</div>
                                </div>
                                <div className="stat-card">
                                    <MessageSquare size={24} />
                                    <div className="stat-value">{formatNumber(overallStats.total_chats)}</div>
                                    <div className="stat-label">Total Chats</div>
                                </div>
                                <div className="stat-card">
                                    <Zap size={24} />
                                    <div className="stat-value">{formatNumber(overallStats.total_tokens_this_month)}</div>
                                    <div className="stat-label">Tokens This Month</div>
                                </div>
                                <div className="stat-card">
                                    <BarChart2 size={24} />
                                    <div className="stat-value">{overallStats.active_users_this_month}</div>
                                    <div className="stat-label">Active Users</div>
                                </div>
                            </div>
                        )}
                        
                        {/* Sort & Filter */}
                        <div className="filter-bar">
                            <label>Sort by:</label>
                            <select value={sortBy} onChange={(e) => setSortBy(e.target.value)}>
                                <option value="total_tokens">Total Tokens</option>
                                <option value="chat_count">Chat Count</option>
                                <option value="message_count">Message Count</option>
                            </select>
                        </div>
                        
                        {/* User Stats Table */}
                        <div className="data-table">
                            <table>
                                <thead>
                                    <tr>
                                        <th>User</th>
                                        <th>Role</th>
                                        <th>Chats</th>
                                        <th>Messages</th>
                                        <th>Input Tokens</th>
                                        <th>Output Tokens</th>
                                        <th>Total Tokens</th>
                                        <th>Quota Used</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {userStats.map(stat => (
                                        <tr key={stat.user_id}>
                                            <td><strong>{stat.username}</strong></td>
                                            <td>{stat.role}</td>
                                            <td>{stat.chat_count}</td>
                                            <td>{stat.message_count}</td>
                                            <td>{formatNumber(stat.total_input_tokens)}</td>
                                            <td>{formatNumber(stat.total_output_tokens)}</td>
                                            <td>{formatNumber(stat.total_tokens)}</td>
                                            <td>
                                                {stat.token_quota === -1 ? (
                                                    <span className="unlimited">∞</span>
                                                ) : (
                                                    <div className="quota-bar">
                                                        <div 
                                                            className="quota-fill" 
                                                            style={{ width: `${Math.min(100, (stat.current_month_tokens / stat.token_quota) * 100)}%` }}
                                                        />
                                                        <span>{Math.round((stat.current_month_tokens / stat.token_quota) * 100)}%</span>
                                                    </div>
                                                )}
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                        
                        {hasMoreStats && (
                            <button className="btn-load-more" onClick={() => fetchUserStats(statsOffset)} disabled={loading}>
                                {loading ? 'Loading...' : 'Load More'}
                            </button>
                        )}
                    </div>
                )}

                {/* Models Tab */}
                {activeTab === TABS.models && (
                    <div className="admin-section">
                        <div className="section-header">
                            <h1>Model Configuration</h1>
                            <div className="section-actions">
                                <button className="btn-secondary" onClick={reloadModels}>
                                    <RefreshCw size={16} /> Reload from YAML
                                </button>
                                <button className="btn-primary" onClick={() => setNewModel({
                                    id: '',
                                    name: '',
                                    provider: 'azure',
                                    deployment: '',
                                    description: '',
                                    context_window: 128000,
                                    max_output: 16384,
                                    supports_reasoning: false,
                                    supports_vision: false,
                                    enabled: true,
                                })}>
                                    <Plus size={16} /> Add Model
                                </button>
                            </div>
                        </div>
                        
                        {modelsConfig && (
                            <>
                                <div className="config-info">
                                    <span>Default Model: <strong>{modelsConfig.default_model}</strong></span>
                                </div>
                                
                                <div className="models-grid">
                                    {Object.entries(modelsConfig.models || {}).map(([modelId, model]) => (
                                        <div key={modelId} className={`model-card ${!model.enabled ? 'disabled' : ''}`}>
                                            <div className="model-card-header">
                                                <Bot size={20} />
                                                <h3>{model.name}</h3>
                                                <div className="model-badges">
                                                    {modelsConfig.tiers?.free?.includes(modelId) && (
                                                        <span className="badge free">Free</span>
                                                    )}
                                                    {modelsConfig.default_model === modelId && (
                                                        <span className="badge default">Default</span>
                                                    )}
                                                </div>
                                            </div>
                                            <div className="model-card-body">
                                                <p className="model-id">{modelId}</p>
                                                <p>{model.description || 'No description'}</p>
                                                <div className="model-specs">
                                                    <span>Provider: {model.provider}</span>
                                                    <span>Context: {formatNumber(model.context_window)}</span>
                                                    {model.supports_reasoning && <span>🧠 Reasoning</span>}
                                                    {model.supports_vision && <span>👁️ Vision</span>}
                                                </div>
                                            </div>
                                            <div className="model-card-actions">
                                                <button onClick={() => setDefaultModel(modelId)} disabled={modelsConfig.default_model === modelId}>
                                                    Set Default
                                                </button>
                                                <button onClick={() => toggleModelTier(modelId, !modelsConfig.tiers?.free?.includes(modelId))}>
                                                    {modelsConfig.tiers?.free?.includes(modelId) ? 'Make Paid' : 'Make Free'}
                                                </button>
                                                <button onClick={() => setEditingModel({ id: modelId, ...model })}>
                                                    <Edit2 size={14} />
                                                </button>
                                                <button className="danger" onClick={() => deleteModel(modelId)}>
                                                    <Trash2 size={14} />
                                                </button>
                                            </div>
                                        </div>
                                    ))}
                                </div>
                            </>
                        )}
                    </div>
                )}
            </main>

            {/* Model Edit Modal */}
            {(editingModel || newModel) && (
                <div className="modal-overlay" onClick={() => { setEditingModel(null); setNewModel(null); }}>
                    <div className="modal" onClick={(e) => e.stopPropagation()}>
                        <h2>{newModel ? 'Add New Model' : 'Edit Model'}</h2>
                        <ModelForm
                            model={newModel || editingModel}
                            isNew={!!newModel}
                            onSave={(id, data) => saveModel(id, data)}
                            onCancel={() => { setEditingModel(null); setNewModel(null); }}
                        />
                    </div>
                </div>
            )}
        </div>
    );
}

// Model Edit Form Component
function ModelForm({ model, isNew, onSave, onCancel }) {
    const [formData, setFormData] = useState({
        id: model.id || '',
        name: model.name || '',
        provider: model.provider || 'azure',
        deployment: model.deployment || '',
        description: model.description || '',
        context_window: model.context_window || 128000,
        max_output: model.max_output || 16384,
        supports_reasoning: model.supports_reasoning || false,
        supports_vision: model.supports_vision || false,
        enabled: model.enabled !== false,
    });

    const handleSubmit = (e) => {
        e.preventDefault();
        const { id, ...data } = formData;
        onSave(id, data);
    };

    return (
        <form onSubmit={handleSubmit} className="model-form">
            {isNew && (
                <div className="form-group">
                    <label>Model ID</label>
                    <input
                        type="text"
                        value={formData.id}
                        onChange={(e) => setFormData({ ...formData, id: e.target.value })}
                        placeholder="e.g., gpt-4o"
                        required
                    />
                </div>
            )}
            <div className="form-group">
                <label>Display Name</label>
                <input
                    type="text"
                    value={formData.name}
                    onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                    placeholder="e.g., GPT-4o"
                    required
                />
            </div>
            <div className="form-row">
                <div className="form-group">
                    <label>Provider</label>
                    <select
                        value={formData.provider}
                        onChange={(e) => setFormData({ ...formData, provider: e.target.value })}
                    >
                        <option value="azure">Azure</option>
                        <option value="openai">OpenAI</option>
                        <option value="anthropic">Anthropic</option>
                        <option value="google">Google</option>
                    </select>
                </div>
                <div className="form-group">
                    <label>Deployment Name</label>
                    <input
                        type="text"
                        value={formData.deployment}
                        onChange={(e) => setFormData({ ...formData, deployment: e.target.value })}
                        placeholder="Azure deployment name"
                    />
                </div>
            </div>
            <div className="form-group">
                <label>Description</label>
                <textarea
                    value={formData.description}
                    onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                    placeholder="Brief description of the model"
                    rows={2}
                />
            </div>
            <div className="form-row">
                <div className="form-group">
                    <label>Context Window</label>
                    <input
                        type="number"
                        value={formData.context_window}
                        onChange={(e) => setFormData({ ...formData, context_window: parseInt(e.target.value) })}
                    />
                </div>
                <div className="form-group">
                    <label>Max Output</label>
                    <input
                        type="number"
                        value={formData.max_output}
                        onChange={(e) => setFormData({ ...formData, max_output: parseInt(e.target.value) })}
                    />
                </div>
            </div>
            <div className="form-row checkboxes">
                <label className="checkbox">
                    <input
                        type="checkbox"
                        checked={formData.supports_reasoning}
                        onChange={(e) => setFormData({ ...formData, supports_reasoning: e.target.checked })}
                    />
                    Supports Reasoning
                </label>
                <label className="checkbox">
                    <input
                        type="checkbox"
                        checked={formData.supports_vision}
                        onChange={(e) => setFormData({ ...formData, supports_vision: e.target.checked })}
                    />
                    Supports Vision
                </label>
                <label className="checkbox">
                    <input
                        type="checkbox"
                        checked={formData.enabled}
                        onChange={(e) => setFormData({ ...formData, enabled: e.target.checked })}
                    />
                    Enabled
                </label>
            </div>
            <div className="form-actions">
                <button type="button" className="btn-secondary" onClick={onCancel}>Cancel</button>
                <button type="submit" className="btn-primary"><Save size={16} /> Save</button>
            </div>
        </form>
    );
}

export default Admin;
