# Integrations

Integration clients that connect external platforms to RAGuardian.

## Architecture Principles

| Rule | Description |
|------|-------------|
| **Server-side API key** | The RAGuardian API key is stored server-side (plugin config, env var, etc.). It never reaches browser JavaScript. |
| **Single scoped API key** | Each installation uses one dedicated API key, granting only the scopes it needs (`query`, `ingest`, `speech`). Do not split a normal integration across multiple keys unless there is a specific operational reason. |
| **Workspace isolation** | The integration routes to one user/workspace via the API key. That workspace's documents, conversations, and index answer the integration. |
| **Self-contained** | Each integration is a self-contained package that can be installed without modifying RAGuardian core. |
| **Test-driven** | E2E tests with a fake RAG server verify the contract. Unit tests verify individual modules. |

## Current Integrations

| Platform | Path | Status |
|----------|------|--------|
| WordPress | `wordpress/rag-client/` | ✅ v0.5.2 - Production |

## Adding a New Integration

1. Copy the `TEMPLATE/` directory as `integrations/<platform>/`.
2. Follow the WordPress plugin as reference for the modular structure.
3. Add a build/install script that produces a single deployable artifact, such as a WordPress zip or platform-specific package.
4. Minimum features: API client with retry, config, query endpoint, health check, rate limit, tests.
5. Add a row in this README under *Current Integrations*.

### Modular Structure Pattern (WordPress reference)

```
integrations/<platform>/
├── README.md              # Standalone install guide
├── plugin.php             # Bootstrap entry point
├── includes/               # Modular classes
│   ├── autoload.php       # Autoloader
│   ├── version.php        # Single version source
│   ├── class-*.php        # Feature classes
│   └── composer.json      # PHPUnit (if PHP)
├── tests/
│   ├── e2e/               # E2E with fake RAG server
│   ├── fixtures/          # Test fixtures
│   └── unit/              # PHPUnit tests
└── package.json           # Dev dep / test harness
```

### Minimum Feature Requirements

| Feature | Description |
|---------|-------------|
| Config | Store RAGuardian URL + one scoped API key securely |
| Query | POST `/api/v1/query` with user intent + conversation ID |
| Retry | Exponential backoff MAX_RETRIES=3, INITIAL_DELAY=1s |
| Health check | Manual and/or periodic `/api/v1/health` with admin-visible failure status |
| Rate limit | Server-side rate limiting per client identity |
| Ingest | Large imports should use local batching and RAGuardian async endpoints instead of one long blocking request |
| Test | E2E (fake RAG) + unit tests for modules |

## Supported RAGuardian Endpoints

| Endpoint | Method | Scope | Description |
|----------|--------|-------|-------------|
| `/api/v1/health` | GET | `query` | Health check |
| `/api/v1/query` | POST | `query` | Chat query |
| `/api/v1/tts` | POST | `speech` | Text-to-speech |
| `/api/v1/files` | POST | `ingest` | Upload file |
| `/api/v1/files/<path>` | DELETE | `ingest` | Delete file |
| `/api/v1/audio` | POST | `ingest` | Upload audio |

## Platform Roadmap

| Platform | Priority | Notes |
|----------|----------|-------|
| Slack | Medium | Slash command + DM bot |
| Notion | Low | Database sync + inline chat |
| Confluence | Low | Space sync + inline widget |
| Discord | Low | Slash command bot |

## Version Compatibility

| RAGuardian version | Integration compat |
|-------------------|-------------------|
| v0.x | WordPress plugin v0.5.0+ |

## License

MIT. See repository `LICENSE` file.
