import { useState } from 'react';
import {
    Brain, Terminal, FileText, Search, Wrench, ListChecks,
    Bot, ArrowLeftRight, Layers, RefreshCw, ChevronDown,
    Check, Circle, CircleDot, Sparkles
} from 'lucide-react';
import { renderMarkdown } from '../lib/markdown';
import AttachmentList from './AttachmentList';

/* Pick an icon + accent for a tool call based on its name. */
function toolIcon(name = '') {
    const n = name.toLowerCase();
    if (/write|edit|read|file/.test(n)) return { Icon: FileText, accent: 'amber' };
    if (/search|grep|glob/.test(n)) return { Icon: Search, accent: 'blue' };
    if (/shell|bash|exec/.test(n)) return { Icon: Terminal, accent: 'slate' };
    if (/task|subagent/.test(n)) return { Icon: Bot, accent: 'violet' };
    return { Icon: Wrench, accent: 'slate' };
}

function StepRow({ Icon, accent = 'slate', children }) {
    return (
        <div className={`astep accent-${accent}`}>
            <div className="astep-rail">
                <div className="astep-ico"><Icon size={14} /></div>
                <div className="astep-line" />
            </div>
            <div className="astep-body">{children}</div>
        </div>
    );
}

function TodoList({ items }) {
    if (!Array.isArray(items)) return null;
    return (
        <ul className="astep-todos">
            {items.map((it, i) => {
                const status = it.status || it.s;
                const label = it.content || it.l || '';
                const done = status === 'completed' || status === 'done';
                const active = status === 'in_progress' || status === 'active';
                return (
                    <li key={i} className={done ? 'done' : active ? 'active' : ''}>
                        <span className="todo-ic">
                            {done ? <Check size={11} /> : active ? <CircleDot size={11} /> : <Circle size={11} />}
                        </span>
                        <span className="todo-tx">{label}</span>
                    </li>
                );
            })}
        </ul>
    );
}

function Block({ block }) {
    const sub = block.is_subagent ? ' is-sub' : '';

    switch (block.type) {
        case 'text':
            return (
                <StepRow Icon={block.is_subagent ? Bot : Brain} accent={block.is_subagent ? 'violet' : 'slate'}>
                    <div className={`astep-title reasoning${sub}`}>
                        {block.is_subagent && <span className="sa-tag">subagent</span>}
                        {block.text}
                    </div>
                </StepRow>
            );
        case 'tool_call': {
            const { Icon, accent } = toolIcon(block.tool);
            return (
                <StepRow Icon={Icon} accent={accent}>
                    <div className={`astep-title${sub}`}>
                        {block.is_subagent && <span className="sa-tag">subagent</span>}
                        <span className="astep-strong">{block.tool || 'tool'}</span>
                    </div>
                    {block.args && Object.keys(block.args).length > 0 && (
                        <pre className="astep-code">{JSON.stringify(block.args, null, 2)}</pre>
                    )}
                    {block.result !== undefined && block.result !== null && block.result !== '' && (
                        <pre className="astep-out">
                            {typeof block.result === 'string' ? block.result : JSON.stringify(block.result, null, 2)}
                            {block.truncated ? '\n…' : ''}
                        </pre>
                    )}
                </StepRow>
            );
        }
        case 'shell':
            return (
                <StepRow Icon={Terminal} accent="slate">
                    <div className={`astep-title${sub}`}>
                        {block.is_subagent && <span className="sa-tag">subagent</span>}
                        <span className="astep-strong">shell</span>
                    </div>
                    <pre className="astep-code">$ {block.command}</pre>
                </StepRow>
            );
        case 'transfer':
            return (
                <StepRow Icon={ArrowLeftRight} accent="violet">
                    <div className="astep-title">
                        Handoff: <span className="astep-strong">{block.from}</span> → <span className="astep-strong">{block.to}</span>
                    </div>
                </StepRow>
            );
        case 'middleware':
            if (block.kind === 'todos') {
                return (
                    <StepRow Icon={ListChecks} accent="amber">
                        <div className="astep-title"><span className="astep-strong">Task list</span></div>
                        <TodoList items={block.detail} />
                    </StepRow>
                );
            }
            return (
                <StepRow Icon={Sparkles} accent="slate">
                    <div className={`astep-title${sub}`}>
                        {block.kind ? `${block.kind}` : 'event'}
                        {block.detail && typeof block.detail === 'string' ? `: ${block.detail}` : ''}
                    </div>
                </StepRow>
            );
        case 'summarizing':
            return (
                <StepRow Icon={Layers} accent="amber">
                    <div className="astep-title">Compacting context…</div>
                </StepRow>
            );
        case 'model_fallback':
            return (
                <StepRow Icon={RefreshCw} accent="amber">
                    <div className="astep-title">Switched to fallback model</div>
                </StepRow>
            );
        default:
            return null;
    }
}

/**
 * Renders one assistant turn: a collapsible "thinking" timeline followed by
 * the final answer. Works for both live streaming turns and stored history.
 */
export default function AgentTurn({ timeline = [], finalText = '', streaming = false, error = null, files = null, historyId = null }) {
    const [open, setOpen] = useState(streaming);
    const hasTimeline = timeline.length > 0;

    return (
        <div className="agent-turn">
            <div className="agent-turn-head">
                <div className="agent-av-sm"><Bot size={14} /></div>
                <span className="agent-name">AI Agent</span>
            </div>

            {hasTimeline && (
                <div className="steps-group">
                    <button className={`steps-toggle${open ? ' open' : ''}`} onClick={() => setOpen(o => !o)}>
                        <span className="steps-label">
                            {streaming ? 'Working…' : `Worked through ${timeline.length} step${timeline.length === 1 ? '' : 's'}`}
                        </span>
                        <ChevronDown size={13} className="steps-chev" />
                    </button>
                    <div className={`steps-body${open ? ' open' : ''}`}>
                        <div className="stepper">
                            {timeline.map((b, i) => <Block key={i} block={b} />)}
                        </div>
                    </div>
                </div>
            )}

            {!hasTimeline && streaming && !finalText && (
                <div className="init-dots"><span /><span /><span /></div>
            )}

            {finalText && (
                <div
                    className="final-answer"
                    dangerouslySetInnerHTML={{
                        __html: renderMarkdown(finalText) + (streaming ? '<span class="cur"></span>' : ''),
                    }}
                />
            )}

            {files && files.length > 0 && (
                <AttachmentList historyId={historyId} files={files} />
            )}

            {error && <div className="turn-error">{error}</div>}
        </div>
    );
}
