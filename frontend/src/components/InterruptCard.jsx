import { AlertTriangle, Check, X } from 'lucide-react';

/** Best-effort extraction of pending tool actions from a HITL interrupt payload. */
function describeActions(payload) {
    const out = [];
    const visit = (node) => {
        if (!node) return;
        if (Array.isArray(node)) {
            node.forEach(visit);
            return;
        }
        if (typeof node === 'object') {
            const req = node.action_request || node.actionRequest;
            if (req && (req.action || req.tool)) {
                out.push({
                    action: req.action || req.tool,
                    args: req.args || req.arguments || {},
                    description: node.description || '',
                });
            } else if (node.action || node.tool) {
                out.push({ action: node.action || node.tool, args: node.args || {}, description: node.description || '' });
            }
        }
    };
    visit(payload);
    return out;
}

/**
 * Renders a human-in-the-loop approval prompt for a paused agent run.
 * onDecide('approve' | 'reject') resumes the run.
 */
export default function InterruptCard({ interrupt, busy, onDecide }) {
    const payload = interrupt?.payload ?? interrupt;
    const actions = describeActions(payload);

    return (
        <div className="hitl-card animate-fade-in">
            <div className="hitl-head">
                <AlertTriangle size={20} />
                <h3>Approval required</h3>
            </div>

            {actions.length > 0 ? (
                actions.map((a, i) => (
                    <div key={i} className="hitl-action">
                        <div className="hitl-tool">{a.action}</div>
                        {a.description && <p className="hitl-desc">{a.description}</p>}
                        {a.args && Object.keys(a.args).length > 0 && (
                            <pre className="hitl-args">{JSON.stringify(a.args, null, 2)}</pre>
                        )}
                    </div>
                ))
            ) : (
                <pre className="hitl-args">{JSON.stringify(payload, null, 2)}</pre>
            )}

            <div className="hitl-actions">
                <button className="hitl-btn approve" disabled={busy} onClick={() => onDecide('approve')}>
                    <Check size={15} /> Approve
                </button>
                <button className="hitl-btn reject" disabled={busy} onClick={() => onDecide('reject')}>
                    <X size={15} /> Reject
                </button>
            </div>
        </div>
    );
}
