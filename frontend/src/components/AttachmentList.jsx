import { useState } from 'react';
import { useAuth } from './AuthContext';
import { useArtifact } from './ArtifactContext';
import {
    FileText, FileCode, Image as ImageIcon, FileType, File as FileIcon,
    Download, Eye, Loader2,
} from 'lucide-react';

function kindIcon(kind) {
    switch (kind) {
        case 'image': return ImageIcon;
        case 'code': return FileCode;
        case 'pdf': return FileType;
        case 'text': return FileText;
        default: return FileIcon;
    }
}

function AttachmentChip({ historyId, file }) {
    const { apiFetch } = useAuth();
    const { openArtifact } = useArtifact();
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState(null);

    const Icon = kindIcon(file.kind);

    const doDownload = async () => {
        setBusy(true); setError(null);
        try {
            const url = `/chat/${historyId}/file?path=${encodeURIComponent(file.path)}&download=1`;
            const res = await apiFetch(url);
            if (!res.ok) throw new Error(`Could not load file (${res.status})`);
            const blob = await res.blob();
            const objUrl = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = objUrl;
            a.download = file.name;
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(() => URL.revokeObjectURL(objUrl), 4000);
        } catch (e) {
            setError(e.message);
        } finally {
            setBusy(false);
        }
    };

    return (
        <div className="attach">
            <div className="attach-row">
                <span className={`attach-ic kind-${file.kind}`}><Icon size={15} /></span>
                <span className="attach-name" title={file.path}>{file.name}</span>
                {file.action && <span className="attach-badge">{file.action}</span>}
                <div className="attach-actions">
                    <button
                        className="attach-btn"
                        onClick={() => openArtifact(historyId, file)}
                        title="Open in panel"
                    >
                        <Eye size={13} /><span>Preview</span>
                    </button>
                    <button className="attach-btn" onClick={doDownload} disabled={busy} title="Download">
                        {busy ? <Loader2 size={13} className="spin" /> : <Download size={13} />}
                        <span>Download</span>
                    </button>
                </div>
            </div>
            {error && <div className="attach-error">{error}</div>}
        </div>
    );
}

/** Renders the list of files generated during an assistant turn. */
export default function AttachmentList({ historyId, files }) {
    if (!files || !files.length || !historyId) return null;
    return (
        <div className="attach-list">
            <div className="attach-title">
                {files.length} file{files.length === 1 ? '' : 's'} generated
            </div>
            {files.map((f, i) => (
                <AttachmentChip key={`${f.path}-${i}`} historyId={historyId} file={f} />
            ))}
        </div>
    );
}
