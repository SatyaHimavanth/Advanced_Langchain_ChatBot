# Backend Developer Guide

FastAPI, SQLAlchemy, LangChain, LangGraph, and Deep Agents power the backend.
The service exposes JWT authentication, chat streaming over SSE, history,
admin APIs, model configuration, token accounting, and agent persistence.

## Setup

```powershell
cd backend
uv sync
```

Create `backend/.env`:

```env
SQLALCHEMY_DATABASE_URL=sqlite:///./agent.db
STORE_DATABASE_URL=

JWT_SECRET_KEY=replace-this
ADMIN_USERNAME=Admin
ADMIN_PASSWORD=replace-this
DEFAULT_TOKEN_QUOTA=100000

AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com
AZURE_OPENAI_API_KEY=...
OPENAI_API_KEY=...
```

`STORE_DATABASE_URL` should be PostgreSQL for durable LangGraph checkpoints and
long-term memory. If it is empty or non-PostgreSQL, the agent store and
checkpointer are in memory.

Copy the model example:

```powershell
Copy-Item app/models.yaml.example app/models.yaml
```

Then run:

```powershell
uv run main.py
```

API documentation is available at `http://localhost:8000/docs`.

## Model YAML

Each entry supports:

- `provider`: `azure_openai`, `openai`, or another provider accepted by
  LangChain's `init_chat_model`
- `model`: provider model name
- `deployment`: Azure deployment name
- `endpoint` or `base_url`
- `api_version`
- `api_key_env`: preferred key source
- `api_key`: supported for local testing, but should not be committed
- capability, context, output, tier, and enabled metadata

The server builds and caches an agent per selected model. The selected model is
always the primary model for the request. If that provider call fails, the
configured `default_model` is attempted as the fallback. Updating or reloading
model configuration clears the cache so later requests use the new settings.

## Account States

- `pending`: can authenticate only to view the approval waiting screen
- `user`: standard application access
- `admin`: application and admin-console access
- `disabled`: authentication and existing bearer-token use are rejected with a
  contact-administrator message

Role changes to `user` or `admin` approve the account. Role changes to
`pending` or `disabled` mark it unapproved.

All self-registrations are created as `pending`; approval cannot be bypassed
with an environment setting. The approval endpoint requires the administrator
to grant either `user` or `admin`. Admin accounts always use `token_quota=-1`
for unlimited monthly usage.

## Token Accounting

Completed assistant turns persist usage to:

- token columns on `app_chat_messages`
- the current user's `tokens_used_this_month`
- monthly aggregate rows in `app_token_usage`

The stream parser normalizes LangChain `usage_metadata` and provider
`response_metadata.token_usage` formats. Free-tier models still record
historical usage but do not consume the user's quota.

## Important Modules

- `app/server.py`: application lifecycle and agent persistence
- `app/api/routers/chat.py`: SSE chat and usage persistence
- `app/api/routers/auth.py`: registration, login, and refresh
- `app/api/routers/admin.py`: users, statistics, and model administration
- `app/models_config.py`: YAML loading and model metadata
- `app/agents/shared/llms.py`: provider client construction
- `app/agents/shared/streaming.py`: LangGraph stream normalization

## Verification

```powershell
uv run python -m compileall -q app
uv run python -c "from app.api.routers import admin, auth, chat"
```

There is no automated backend test suite yet. Add focused tests around auth
state transitions, token metadata normalization, and monthly usage upserts
before changing those contracts further.
