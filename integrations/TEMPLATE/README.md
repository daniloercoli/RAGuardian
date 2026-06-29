# <Platform> Integration for RAGuardian

## Overview

Connect a <platform> instance to one RAGuardian user/workspace via server-to-server API calls.

The RAGuardian API key is stored server-side and never exposed to browser JavaScript. A normal integration uses one workspace-scoped key with the required scopes.

## Install

1. Copy this integration into your <platform> project.
2. Configure the RAGuardian base URL and one scoped API key.
3. Test connectivity with `/api/v1/health`.

## Configuration

| Setting | Description |
|---------|-------------|
| `RAG_BASE_URL` | RAGuardian server URL |
| `RAG_API_KEY` | Workspace-scoped API key with the required scopes |
| `RAG_TIMEOUT` | Request timeout in seconds (default: 45) |

## Endpoints Used

| Endpoint | Scope |
|----------|-------|
| `/api/v1/health` | `query` |
| `/api/v1/query` | `query` |
| `/api/v1/tts` | `speech` |
| `/api/v1/files` | `ingest` |
| `/api/v1/audio` | `ingest` |

## Testing

```bash
# Install deps
npm install

# Run E2E
npm run test:e2e

# Run unit tests
npm run test:unit
```

## License

MIT
