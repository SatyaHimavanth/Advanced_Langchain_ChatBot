import { createContext, useContext, useState, useCallback } from 'react';

/**
 * Holds the currently-open artifact (a generated file shown in the right-side
 * panel, claude.ai style). `openArtifact(historyId, file)` opens the panel;
 * `closeArtifact()` hides it.
 */
const ArtifactContext = createContext(null);

export function ArtifactProvider({ children }) {
    const [artifact, setArtifact] = useState(null); // { historyId, file } | null

    const openArtifact = useCallback((historyId, file) => {
        setArtifact({ historyId, file });
    }, []);

    const closeArtifact = useCallback(() => setArtifact(null), []);

    return (
        <ArtifactContext.Provider value={{ artifact, openArtifact, closeArtifact }}>
            {children}
        </ArtifactContext.Provider>
    );
}

export function useArtifact() {
    return useContext(ArtifactContext) || {
        artifact: null,
        openArtifact: () => {},
        closeArtifact: () => {},
    };
}
