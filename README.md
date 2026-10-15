# Codex host proxy

Share a host machine's Codex model access with another machine through a Responses API. The client uses its standard `~/.codex/config.toml`; commands, file edits, approvals, and MCP tools run on the client.

```text
Client Codex → host proxy → host credentials → LLM
```

The host needs Python 3.11+ and Codex CLI. There are no Python runtime dependencies. The client needs Codex CLI.

## Host setup

The proxy reads `config.toml` and `auth.json` from `$CODEX_HOME`, defaulting to `~/.codex`. It supports file-based ChatGPT or API-key logins and custom Responses providers.

If the host is already logged in and `auth.json` exists, start the proxy:

```sh
./run.sh serve
```

For a new login or credentials stored only in the OS keyring, log in with file storage first:

```sh
codex -c 'cli_auth_credentials_store="file"' login
```

The server listens on `0.0.0.0:8787` and prints client instructions using its detected LAN IP and configured model. The built-in API key is `sk-codex-proxy`; no token environment variable is required.

The key is shared across installations. Use a trusted network, SSH tunnel, or HTTPS reverse proxy. Plain HTTP does not encrypt conversations or the key.

## Client setup

Merge the configuration printed by the host into the client's `~/.codex/config.toml` (or `$CODEX_HOME/config.toml`). Replace existing keys instead of duplicating them, and put root keys before TOML tables.

For example, with a host at `192.168.1.10`:

```toml
model = "YOUR_HOST_MODEL"
model_provider = "codex_proxy"

[model_providers.codex_proxy]
name = "Host Codex proxy"
base_url = "http://192.168.1.10:8787/v1"
experimental_bearer_token = "sk-codex-proxy"
model_catalog_url = "http://192.168.1.10:8787/v1/codex-models"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
```

Use the address and model printed by your host, then start a new client session:

```sh
codex
```

The client does not need an OpenAI login or a copy of the host's credentials. Its sandbox and approval settings still apply.

To print the client configuration separately on the host:

```sh
./run.sh client-config
```

## Options

| Purpose | Command |
| --- | --- |
| Change the listening port | `./run.sh serve --port 9000` |
| Use another Codex home | `./run.sh --codex-home /path/to/.codex serve` |
| Select a host profile | `./run.sh --profile work serve` |
| Change the client endpoint | `./run.sh client-config --base-url https://proxy.example.com/v1` |
| Choose a client model | `./run.sh client-config --model YOUR_MODEL` |
| Set the Codex executable | `./run.sh serve --codex-bin /path/to/codex` |
| Set the upstream idle timeout | `./run.sh serve --upstream-timeout 300` |

Profiles support `<name>.config.toml` files and legacy `[profiles.<name>]` tables. Restart the proxy after changing host configuration. When changing the listening port, use that port in the client URLs too.

For an SSH tunnel, run this on the client and leave it open:

```sh
ssh -N -L 8787:127.0.0.1:8787 user@HOST
```

Generate the corresponding client configuration on the host:

```sh
./run.sh client-config --base-url http://127.0.0.1:8787/v1
```

## API and behavior

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/v1/responses` | Model requests with HTTP streaming |
| POST | `/v1/responses/compact` | Conversation compaction |
| GET | `/v1/models` | Upstream model list |
| GET | `/v1/codex-models` | Host's Codex model metadata |
| GET | `/healthz` | Proxy liveness |

API routes require `Authorization: Bearer sk-codex-proxy`; `/healthz` does not. The `/v1` prefix is optional for API routes. WebSocket transport is not supported. Request bodies are limited to 64 MiB.

Host credentials replace the client's proxy key before forwarding. Credentials reload on each request; expired ChatGPT logins are renewed through `codex app-server`. OS-keyring-only and ephemeral credentials are unsupported. Run one proxy process per host login to avoid competing token refreshes.

The metadata endpoint serves the host's configured `model_catalog_json`, or the models from `models_cache.json`, without cache identity fields. Tool and conversation payloads pass through; the proxy does not run jobs on the host. Logs include stream diagnostics, but not tokens or conversation payloads.

Upstream connections respect `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`, and `CODEX_CA_CERTIFICATE`.

## Troubleshooting

- **Old sessions fail, new sessions work:** start a new session after switching to the proxy. Try this before changing model or transport settings.
- **Missing model metadata:** confirm the client has the printed `model_catalog_url`. Run Codex on the host if its model cache is missing. For clients needing a local catalog, follow the download instructions printed at proxy startup.
- **Client cannot parse the catalog:** align the client and host Codex versions.
- **Stream disconnects:** inspect the host log. `terminal=response.completed` indicates completion; `terminal=missing` indicates an incomplete stream. Early failures include upstream status, encoding, and `reason=...`.

Check that the server is running with:

```sh
curl http://127.0.0.1:8787/healthz
```

This checks liveness only. A successful client conversation verifies model access and streaming.

## Development

Run tests with a Python 3.11+ interpreter:

```sh
python3 -m unittest discover -s tests -v
```

If local network listeners are restricted, use socket pairs:

```sh
CODEX_PROXY_TEST_TRANSPORT=socketpair python3 -m unittest discover -s tests -v
```

Tests use temporary credentials and mock upstreams; they do not call the LLM or modify the host login.

Optional package installation provides a `codex-proxy` command in place of `./run.sh`:

```sh
python3 -m pip install .
```

See OpenAI's [provider configuration](https://developers.openai.com/codex/config-advanced), [authentication](https://developers.openai.com/codex/auth), and [gateway requirements](https://learn.chatgpt.com/docs/enterprise/gateway-compatibility) for configuration details.
