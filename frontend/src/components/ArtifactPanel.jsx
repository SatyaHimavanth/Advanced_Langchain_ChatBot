import { useState, useEffect, useCallback, useRef } from 'react';
import { useAuth } from './AuthContext';
import { useArtifact } from './ArtifactContext';
import {
    X, Copy, Check, Download, Play, Eye, Code2, Loader2, Square,
    FileCode, FileText, Image as ImageIcon, FileType, File as FileIcon,
} from 'lucide-react';

const HTML_EXTS = ['.html', '.htm'];

function extOf(name = '') {
    const i = name.lastIndexOf('.');
    return i >= 0 ? name.slice(i).toLowerCase() : '';
}

function isRunnable(name) {
    return ['.py', '.js', '.mjs', '.cjs', '.sh', '.bash'].includes(extOf(name));
}

function kindIcon(kind) {
    switch (kind) {
        case 'image': return ImageIcon;
        case 'code': return FileCode;
        case 'pdf': return FileType;
        case 'text': return FileText;
        default: return FileIcon;
    }
}

export default function ArtifactPanel() {
    const { apiFetch } = useAuth();
    const { artifact, closeArtifact } = useArtifact();

    const [content, setContent] = useState('');     // text content
    const [imgUrl, setImgUrl] = useState(null);      // object URL for images/pdf
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(null);
    const [copied, setCopied] = useState(false);
    const [mode, setMode] = useState('preview');     // 'preview' | 'code'
    const [runOut, setRunOut] = useState(null);      // {stdout, stderr, exit_code, timed_out}
    const [running, setRunning] = useState(false);
    const objUrlRef = useRef(null);

    const file = artifact?.file;
    const historyId = artifact?.historyId;
    const ext = extOf(file?.name);
    const isHtml = HTML_EXTS.includes(ext);
    const isImage = file?.kind === 'image';
    const isPdf = file?.kind === 'pdf';
    const runnable = file && isRunnable(file.name);

    const cleanupObjUrl = () => {
        if (objUrlRef.current) {
            URL.revokeObjectURL(objUrlRef.current);
            objUrlRef.current = null;
        }
    };

    const load = useCallback(async () => {
        if (!file || !historyId) return;
        setLoading(true);
        setError(null);
        setRunOut(null);
        setContent('');
        cleanupObjUrl();
        setImgUrl(null);
        // HTML opens in preview by default; everything else shows content.
        setMode(HTML_EXTS.includes(extOf(file.name)) ? 'preview' : 'code');
        try {
            const res = await apiFetch(`/chat/${historyId}/file?path=${encodeURIComponent(file.path)}`);
            if (!res.ok) throw new Error(`Could not load file (${res.status})`);
            if (file.kind === 'image' || file.kind === 'pdf') {
                const blob = await res.blob();
                const url = URL.createObjectURL(blob);
                objUrlRef.current = url;
                setImgUrl(url);
            } else {
                setContent(await res.text());
            }
        } catch (e) {
            setError(e.message);
        } finally {
            setLoading(false);
        }
    }, [apiFetch, file, historyId]);

    useEffect(() => {
        load();
        return cleanupObjUrl;
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [load]);

    const copy = async () => {
        try {
            await navigator.clipboard.writeText(content);
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
        } catch {
            /* clipboard may be blocked */
        }
    };

    const download = async () => {
        try {
            const res = await apiFetch(`/chat/${historyId}/file?path=${encodeURIComponent(file.path)}&download=1`);
            const blob = await res.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url; a.download = file.name;
            document.body.appendChild(a); a.click(); a.remove();
            setTimeout(() => URL.revokeObjectURL(url), 4000);
        } catch (e) {
            setError(e.message);
        }
    };

    const run = async () => {
        setRunning(true);
        setRunOut(null);
        setError(null);
        try {
            const res = await apiFetch(`/chat/${historyId}/run`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: file.path }),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || `Run failed (${res.status})`);
            setRunOut(data);
        } catch (e) {
            setError(e.message);
        } finally {
            setRunning(false);
        }
    };

    if (!artifact) return null;
    const Icon = kindIcon(file.kind);

    return (
        <aside className="artifact-panel">
            <header className="artifact-head">
                <div className="artifact-title">
                    <span className={`artifact-ic kind-${file.kind}`}><Icon size={15} /></span>
                    <span className="artifact-name" title={file.path}>{file.name}</span>
                </div>
                <div className="artifact-tools">
                    {isHtml && !loading && (
                        <div className="artifact-seg">
                            <button
                                className={mode === 'preview' ? 'on' : ''}
                                onClick={() => setMode('preview')}
                                title="Preview"
                            >
                                <Eye size={13} /> Preview
                            </button>
                            <button
                                className={mode === 'code' ? 'on' : ''}
                                onClick={() => setMode('code')}
                                title="View code"
                            >
                                <Code2 size={13} /> Code
                            </button>
                        </div>
                    )}
                    {runnable && (
                        <button className="artifact-btn" onClick={run} disabled={running} title="Run">
                            {running ? <Loader2 size={14} className="spin" /> : <Play size={14} />}
                            <span>Run</span>
                        </button>
                    )}
                    {!isImage && !isPdf && (
                        <button className="artifact-btn" onClick={copy} title="Copy contents">
                            {copied ? <Check size={14} /> : <Copy size={14} />}
                            <span>{copied ? 'Copied' : 'Copy'}</span>
                        </button>
                    )}
                    <button className="artifact-btn" onClick={download} title="Download">
                        <Download size={14} />
                    </button>
                    <button className="artifact-close" onClick={closeArtifact} title="Close">
                        <X size={16} />
                    </button>
                </div>
            </header>

            <div className="artifact-body">
                {loading && (
                    <div className="artifact-loading"><Loader2 size={18} className="spin" /> Loading…</div>
                )}
                {error && <div className="artifact-error">{error}</div>}

                {!loading && !error && (
                    <>
                        {isImage && imgUrl && (
                            <div className="artifact-image"><img src={imgUrl} alt={file.name} /></div>
                        )}
                        {isPdf && imgUrl && (
                            <iframe className="artifact-frame" title={file.name} src={imgUrl} />
                        )}
                        {isHtml && mode === 'preview' && (
                            <iframe
                                className="artifact-frame"
                                title={file.name}
                                sandbox="allow-scripts allow-same-origin"
                                srcDoc={content}
                            />
                        )}
                        {((isHtml && mode === 'code') || (!isHtml && !isImage && !isPdf)) && (
                            <pre className="artifact-code">{content}</pre>
                        )}
                    </>
                )}

                {runOut && (
                    <div className="artifact-run">
                        <div className="artifact-run-head">
                            <span>Output</span>
                            {runOut.timed_out
                                ? <span className="run-badge timeout">timed out</span>
                                : <span className={`run-badge ${runOut.exit_code === 0 ? 'ok' : 'err'}`}>
                                    exit {runOut.exit_code}
                                  </span>}
                            <button className="run-close" onClick={() => setRunOut(null)} title="Clear">
                                <Square size={11} />
                            </button>
                        </div>
                        {runOut.stdout && <pre className="run-out">{runOut.stdout}</pre>}
                        {runOut.stderr && <pre className="run-out err">{runOut.stderr}</pre>}
                        {!runOut.stdout && !runOut.stderr && (
                            <div className="run-empty">(no output)</div>
                        )}
                    </div>
                )}
            </div>
        </aside>
    );
}
