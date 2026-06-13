# Frontend Developer Guide

The frontend is a React 19 and Vite application for authentication, streaming
agent chat, generated artifacts, conversation history, model selection, quota
display, and administration.

## Setup

```powershell
cd frontend
npm install
npm run dev
```

The development URL is `http://localhost:5173`.

Set a different backend URL with:

```env
VITE_API_BASE=http://localhost:8000
```

## Scripts

```powershell
npm run dev
npm run build
npm run lint
npm run preview
```

## Main Areas

- `src/pages/Login.jsx`: sign-in and registration
- `src/pages/Dashboard.jsx`: chat, histories, quota, model selection, and SSE
- `src/pages/Admin.jsx`: approvals, account roles, usage, and model settings
- `src/components/AuthContext.jsx`: JWT storage, refresh, and API wrapper
- `src/lib/agentStream.js`: streaming event parser
- `src/App.css`: shared application and admin styles

## Admin Behavior

Pending registrations are shown in an approval table. The administrator
selects User or Admin access before choosing Approve, or can reject the request.
Approved accounts can later be assigned `user`, `admin`, or `disabled`.
The disabled state is shown separately from pending and the backend prevents
the account from signing in. Admin access displays unlimited quota because the
backend enforces `token_quota=-1`.

The model editor supports provider model names, Azure deployment names,
endpoints, API versions, API-key environment variable names, and
OpenAI-compatible base URLs. It does not need the secret value when
`api_key_env` is configured on the backend.

## Streaming Contract

The chat endpoint returns server-sent events such as `text_delta`,
`tool_call_start`, `tool_result`, `interrupt`, `files`, `token_usage`, `error`,
and `done`. Keep changes to `agentStream.js` synchronized with
`backend/app/agents/shared/streaming.py`.

## Verification

```powershell
npm run build
npm run lint
```

The production build currently passes. Lint also reports existing issues in
unrelated files, including `App.jsx`, `AuthContext.jsx`, `AgentTurn.jsx`,
`ArtifactContext.jsx`, and `markdown.js`; resolve those separately rather than
hiding them with broad disables.
