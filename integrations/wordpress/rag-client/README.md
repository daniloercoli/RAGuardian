# Raguardian for WordPress

Ready-to-install WordPress plugin for connecting a public site to one RAGuardian user workspace through server-to-server API calls.

The plugin is intentionally a server-side client: the RAGuardian API key is stored in WordPress options, used only by PHP, and never exposed to browser JavaScript. In multi-user RAGuardian deployments, that API key identifies the user/workspace whose documents, data sources, conversations, and Chroma collection answer the public chatbot.

## Install

1. Build an installable zip from this directory:

```bash
cd integrations/wordpress/rag-client
npm install
npm run build:zip
```

2. Install the generated `dist/rag-client.zip` through **Plugins -> Add New -> Upload Plugin**, or with WP-CLI:

```bash
wp plugin install dist/rag-client.zip --activate
```

3. Open **Settings -> Raguardian**.
4. Configure the RAGuardian base URL and paste an API key created from **RAGuardian -> API Keys** for the workspace user that should power the website.
5. Enable the floating widget globally or place the inline shortcode where needed:

```text
[rag_chat]
```

Recommended setup:

1. In RAGuardian, create a local user such as `website@example.com`.
2. Log in as that user or as admin and create one API key for that user's workspace.
3. Give the key `query` scope for chat and `ingest` scope for article import/sync and audio upload. Add `speech` only if TTS is enabled.
4. Add that key to **Settings -> Raguardian** in WordPress.
5. Use **Test RAGuardian health** to verify that WordPress can reach `/api/v1/health`.

Do not create separate keys for chat, ingestion, and speech unless you intentionally want separate rotation or audit boundaries. The normal plugin setup uses one key with the required scopes.

## Architecture

The bootstrap file `rag-client.php` only wires the plugin together. Runtime behavior lives in focused classes under `includes/`:

| Class | Responsibility |
|-------|----------------|
| `EC_Rag_Api_Client` | Server-to-server HTTP client, auth header, timeout, multipart upload, retry/backoff |
| `EC_Rag_Options` and `EC_Rag_Settings_Form` | Settings registration, sanitization, and admin UI |
| `EC_Rag_Widget` | Floating widget, shortcode rendering, asset loading, inline config |
| `EC_Rag_Ajax` | Chat, TTS, and audio upload AJAX proxy endpoints |
| `EC_Rag_Ingestion` | WXR import queue and public-post live synchronization hooks |
| `EC_Rag_Rate_Limiter` | WordPress transient-based abuse throttling |
| `EC_Rag_Health_Check` | Periodic connectivity check, admin bar state, admin notices |
| `EC_Rag_Utils` | Safe article extraction, page context, file path and text helpers |

## Floating Chatbot

The plugin can render a classic bottom-corner chatbot from `wp_footer`.

Admin options include:

- widget title, launcher label, welcome message and input placeholder;
- status label and optional privacy note inside the chat panel;
- primary/text colors and optional avatar/logo URL;
- theme-inherited or custom appearance mode;
- bottom-right or bottom-left position;
- source visibility, response language policy, request timeout, optional TTS button, and optional audio upload;
- visibility for logged-in users only or also anonymous visitors;
- page/post exclusions by ID, slug or path;
- WordPress-side rate limits for chat, TTS, and audio upload;
- optional custom CSS namespaced under `.ec-rag-*`.

The chat header includes compact controls for downloading the current conversation as `.txt` and minimizing the panel. The TXT export is generated in the browser from the visible conversation transcript and does not expose the RAGuardian API key.

## Article Ingestion

The plugin has two article ingestion sources plus a local batching step:

1. **Initial import from WordPress export**: In WordPress, go to **Tools -> Export**, download the standard WordPress XML/WXR export, then upload it in **Settings -> Raguardian -> Initial WordPress export import**. The plugin parses the WXR file, extracts only public `post` articles, ignores password-protected content, author records, author emails, comments, user data, and post meta, and stores a local JSONL queue of sanitized text snapshots.
2. **Queued batch processing**: Import work is processed in small WordPress cron batches, with a manual **Process next batch** fallback in the settings page. This avoids one long blocking request for large sites.
3. **Live synchronization**: When **Keep public articles synchronized** is enabled, WordPress hooks update RAGuardian when a public article is published, updated, unpublished, password-protected, or deleted.

Snapshots are uploaded to RAGuardian with stable paths such as:

```text
wordpress/posts/post-123.txt
```

Snapshots are uploaded through `/api/v1/files?async=true`. Large imports should use the RAGuardian async upload path with Redis/RQ enabled:

```env
RAG_QUEUE_BACKEND=redis
REDIS_URL=redis://127.0.0.1:6379/0
RAG_QUEUE_NAME=rag-default
```

Worker:

```bash
PYTHONPATH=app rq worker rag-default
```

The plugin uses standard WordPress functions, actions, filters, uploads, options, post hooks, AJAX, and WP-Cron. It does not query the WordPress database directly for article content.

## Health, Retry, and Abuse Controls

- The settings page has a manual **Test RAGuardian health** button.
- A periodic WP-Cron health check calls `/api/v1/health` every hour when the plugin is configured.
- Administrators see the current connection state in the admin bar.
- After repeated failures, the plugin shows an admin notice and logs the error.
- API calls retry transient network failures, HTTP 408/429, and 5xx responses with exponential backoff.
- Chat, TTS, and audio upload each have configurable WordPress-side rate limits.
- Query text length, TTS text length, audio file extension, audio size, nonces, and permissions are validated before proxying requests.

## Audio and Speech

The chatbot is ready for optional voice workflows:

- `enable_tts` shows a text-to-speech button and calls `/api/v1/tts` with the same server-side API key.
- `enable_audio_upload` shows an audio upload control and sends accepted audio files to `/api/v1/audio?async=true`.
- Supported upload extensions are `mp3`, `wav`, `m4a`, `webm`, `ogg`, and `flac`.
- Audio upload uses `ingest` scope because the transcript is indexed into the workspace. TTS uses `speech` scope.

## Automated WordPress Testing

This plugin includes a local WordPress test harness based on `@wordpress/env`, Playwright, and a fake RAG service.

Prerequisites:

- Docker Desktop running;
- Node.js/npm available;
- Composer available for PHPUnit dependencies;
- PHP available at `/usr/local/bin/php` for syntax checks.

On macOS, `brew install docker` installs only the Docker CLI. For `wp-env` you also need a running Docker daemon and Compose V2. The simplest setup is:

```bash
brew install --cask docker
open -a Docker
docker compose version
docker info
```

First-time setup:

```bash
cd integrations/wordpress/rag-client
npm install
npm run composer:install
npx playwright install chromium
```

Run the WordPress environment and e2e suite:

```bash
npm run test:e2e
```

Useful commands:

```bash
npm run doctor
npm run build:zip
npm run wp:start
npm run wp:setup
npm run wp:logs
npm run wp:stop
npm run test
npm run test:e2e:headed
npm run test:php
```

The fake RAG server listens on `127.0.0.1:5055` for test inspection. WordPress runs inside Docker, so the plugin is configured during tests with:

```text
http://host.docker.internal:5055
```

The included WXR fixture lives at:

```text
tests/fixtures/wp-export-public-posts.xml
```

When a real WordPress export is available, add a sanitized fixture or a private local-only fixture and extend the Playwright import spec. Do not commit production exports containing personal data.

## Shortcode Overrides

The shortcode remains backward-compatible and supports local overrides:

```text
[rag_chat title="Supporto" context="Il visitatore è nella pagina servizi" show_sources="1" enable_tts="0" response_language="it"]
```

`enable_tts="1"` can show the button only when TTS is enabled in plugin settings and the API key has `speech` scope.

`response_language` accepts `auto`, `it`, or `en`. `auto` lets RAGuardian answer in the visitor question language.

## Client Context

Every query can include safe page metadata:

- site name;
- current page title;
- current page URL;
- post type;
- WordPress locale;
- global admin context and optional shortcode context.

This context is sent from WordPress to RAGuardian in `client_context`. It is used only in the model prompt, is never indexed, and is not returned as a source.

The API key is stored server-side in WordPress options and is never localized into browser JavaScript.

## Multi-User Notes

RAGuardian isolates data by user workspace. The WordPress plugin does not choose a workspace explicitly; the API key does. This makes public websites easy to reason about:

- one website can use a dedicated `website` user;
- multiple websites can use different RAGuardian users and API keys;
- a site's questions never query another user's files, email connector, Drive connector, or conversations;
- rotating a key in RAGuardian immediately changes the WordPress integration boundary.

## License

MIT. See the repository `LICENSE` file.
