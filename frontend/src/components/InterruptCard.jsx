import { useMemo, useState } from 'react';
import { AlertTriangle, Check, Pencil, X } from 'lucide-react';

function prettifyLabel(key) {
    return String(key)
        .replace(/[_-]+/g, ' ')
        .replace(/([a-z])([A-Z])/g, '$1 $2')
        .replace(/\b\w/g, (ch) => ch.toUpperCase());
}

function stringifyValue(value) {
    if (value == null) return '';
    if (typeof value === 'boolean') return value ? 'true' : 'false';
    if (Array.isArray(value)) return value.join('\n');
    if (typeof value === 'object') return JSON.stringify(value, null, 2);
    return String(value);
}

function parseEditedValue(raw, original) {
    if (Array.isArray(original)) {
        return raw
            .split('\n')
            .map((line) => line.trim())
            .filter(Boolean);
    }
    if (typeof original === 'boolean') {
        return raw === 'true';
    }
    if (typeof original === 'number') {
        const num = Number(raw);
        return Number.isNaN(num) ? original : num;
    }
    if (original && typeof original === 'object') {
        return raw.trim() ? JSON.parse(raw) : {};
    }
    return raw;
}

function collectActions(payload) {
    if (!payload || typeof payload !== 'object') return [];

    const reviewConfigs = Array.isArray(payload.review_configs)
        ? payload.review_configs
        : Array.isArray(payload.reviewConfigs)
            ? payload.reviewConfigs
            : [];

    const actionRequests = Array.isArray(payload.action_requests)
        ? payload.action_requests
        : Array.isArray(payload.actionRequests)
            ? payload.actionRequests
            : [];

    const normalizedRequests = actionRequests.map((request) => ({
        action: request?.action || request?.tool || request?.name || request?.action_name || '',
        args: request?.args || request?.arguments || {},
        description: request?.description || '',
        allowedDecisions: [],
    }));

    const normalizedReviews = reviewConfigs.map((config) => ({
        action: config?.action_name || config?.action || config?.tool || config?.name || '',
        args: config?.args || config?.arguments || {},
        description: config?.description || '',
        allowedDecisions: Array.isArray(config?.allowed_decisions)
            ? config.allowed_decisions
            : Array.isArray(config?.allowedDecisions)
                ? config.allowedDecisions
                : [],
    }));

    const merged = [];
    const maxLen = Math.max(normalizedRequests.length, normalizedReviews.length);
    for (let i = 0; i < maxLen; i += 1) {
        const request = normalizedRequests[i] || {};
        const review = normalizedReviews[i] || {};
        merged.push({
            action: request.action || review.action || 'Tool action',
            args: Object.keys(request.args || {}).length ? request.args : (review.args || {}),
            description: request.description || review.description || '',
            allowedDecisions: review.allowedDecisions || [],
        });
    }

    if (merged.length > 0) return merged;

    const req = payload.action_request || payload.actionRequest;
    if (req && (req.action || req.tool || req.name)) {
        return [{
            action: req.action || req.tool || req.name,
            args: req.args || req.arguments || {},
            description: payload.description || req.description || '',
            allowedDecisions: [],
        }];
    }

    if (payload.action || payload.tool || payload.name) {
        return [{
            action: payload.action || payload.tool || payload.name,
            args: payload.args || payload.arguments || {},
            description: payload.description || '',
            allowedDecisions: [],
        }];
    }

    return [];
}

function ParameterField({ name, value, readOnly, draftValue, onChange }) {
    const label = prettifyLabel(name);
    const isArray = Array.isArray(value);
    const isBoolean = typeof value === 'boolean';
    const isObject = value && typeof value === 'object' && !Array.isArray(value);
    const multiline = isArray || isObject || String(draftValue || '').length > 80;

    return (
        <div className="hitl-param-row">
            <div className="hitl-param-label">{label}</div>
            {readOnly ? (
                <div className={`hitl-param-value ${multiline ? 'multiline' : ''}`}>
                    {isArray ? (
                        value.length > 0 ? value.map((item, idx) => (
                            <div key={`${name}-${idx}`}>{String(item)}</div>
                        )) : <span>None</span>
                    ) : isObject ? (
                        <pre className="hitl-param-object">{JSON.stringify(value, null, 2)}</pre>
                    ) : (
                        <span>{stringifyValue(value) || 'Empty'}</span>
                    )}
                </div>
            ) : isBoolean ? (
                <select
                    className="hitl-edit-field"
                    value={draftValue}
                    onChange={(e) => onChange(name, e.target.value)}
                >
                    <option value="true">True</option>
                    <option value="false">False</option>
                </select>
            ) : multiline ? (
                <textarea
                    className="hitl-edit-field hitl-edit-field-area"
                    value={draftValue}
                    onChange={(e) => onChange(name, e.target.value)}
                    rows={isObject ? 6 : 4}
                    spellCheck={false}
                />
            ) : (
                <input
                    className="hitl-edit-field"
                    value={draftValue}
                    onChange={(e) => onChange(name, e.target.value)}
                    spellCheck={false}
                />
            )}
        </div>
    );
}

export default function InterruptCard({ interrupt, busy, onDecide }) {
    const payload = interrupt?.payload ?? interrupt;
    const actions = useMemo(() => collectActions(payload), [payload]);
    const primaryAction = actions[0] || null;
    const canApprove = !primaryAction || primaryAction.allowedDecisions.length === 0 || primaryAction.allowedDecisions.includes('approve');
    const canReject = !primaryAction || primaryAction.allowedDecisions.length === 0 || primaryAction.allowedDecisions.includes('reject');
    const canEdit = !!primaryAction && primaryAction.allowedDecisions.includes('edit');
    const initialDraft = useMemo(() => {
        if (!primaryAction) return {};
        return Object.fromEntries(
            Object.entries(primaryAction.args || {}).map(([key, value]) => [key, stringifyValue(value)])
        );
    }, [primaryAction]);

    const [isEditing, setIsEditing] = useState(false);
    const [draftArgs, setDraftArgs] = useState(initialDraft);
    const [editError, setEditError] = useState('');

    const handleFieldChange = (name, value) => {
        setDraftArgs((prev) => ({ ...prev, [name]: value }));
    };

    const submitEdit = () => {
        if (!primaryAction) return;
        try {
            const parsedArgs = Object.fromEntries(
                Object.entries(primaryAction.args || {}).map(([key, original]) => [
                    key,
                    parseEditedValue(draftArgs[key] ?? '', original),
                ])
            );
            setEditError('');
            onDecide({
                type: 'edit',
                edited_action: {
                    name: primaryAction.action,
                    args: parsedArgs,
                },
            });
        } catch {
            setEditError('One or more parameters are not valid. Please review the edited values.');
        }
    };

    return (
        <div className="hitl-card animate-fade-in">
            <div className="hitl-head">
                <AlertTriangle size={20} />
                <h3>Approval required</h3>
            </div>

            {primaryAction ? (
                <div className="hitl-action">
                    <div className="hitl-tool">{primaryAction.action}</div>
                    {primaryAction.description && <p className="hitl-desc">{primaryAction.description}</p>}

                    <div className="hitl-params">
                        {Object.keys(primaryAction.args || {}).length > 0 ? (
                            Object.entries(primaryAction.args).map(([name, value]) => (
                                <ParameterField
                                    key={name}
                                    name={name}
                                    value={value}
                                    readOnly={!isEditing}
                                    draftValue={draftArgs[name] ?? stringifyValue(value)}
                                    onChange={handleFieldChange}
                                />
                            ))
                        ) : (
                            <div className="hitl-empty-params">This tool has no editable parameters.</div>
                        )}
                    </div>

                    {editError && <div className="hitl-edit-error">{editError}</div>}
                </div>
            ) : (
                <div className="hitl-empty-params">Tool approval details are available, but no editable parameters were provided.</div>
            )}

            <div className="hitl-actions">
                {canApprove && (
                    <button className="hitl-btn approve" disabled={busy} onClick={() => onDecide({ type: 'approve' })}>
                        <Check size={15} /> Approve
                    </button>
                )}
                {canEdit && (
                    isEditing ? (
                        <>
                            <button className="hitl-btn edit" disabled={busy} onClick={submitEdit}>
                                <Pencil size={15} /> Submit edit
                            </button>
                            <button
                                className="hitl-btn neutral"
                                disabled={busy}
                                onClick={() => {
                                    setIsEditing(false);
                                    setEditError('');
                                    setDraftArgs(initialDraft);
                                }}
                            >
                                Cancel
                            </button>
                        </>
                    ) : (
                        <button className="hitl-btn edit" disabled={busy} onClick={() => setIsEditing(true)}>
                            <Pencil size={15} /> Edit
                        </button>
                    )
                )}
                {canReject && (
                    <button className="hitl-btn reject" disabled={busy} onClick={() => onDecide({ type: 'reject' })}>
                        <X size={15} /> Reject
                    </button>
                )}
            </div>
        </div>
    );
}
