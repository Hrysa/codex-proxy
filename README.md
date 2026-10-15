# Codex host proxy

Expose the model access of a logged-in Codex host as a token-protected Responses API. Another machine connects using its normal `~/.codex/config.toml`. Commands, file edits, approvals, and MCP tools run on that client machine.

```text
Client Codex CLI → proxy token → host proxy → host Codex credentials → LLM
```

Python 3.11+ is required, with no runtime dependencies. The host also needs Codex CLI for automatic ChatGPT login renewal. The client only needs Codex CLI.

## Start on the host

The proxy reads `$CODEX_HOME/config.toml` and `$CODEX_HOME/auth.json`, defaulting to `~/.codex`. It supports a file-based ChatGPT login, an OpenAI API key login, or a custom Responses provider with `env_key`, static bearer token, command-backed auth, headers, and query parameters.

If Codex is already logged in and `~/.codex/auth.json` exists, keep that setup. For a new login or an OS-keyring-only login:

```sh
codex -c 'cli_auth_credentials_store="file"' login
```

Start the proxy. Its built-in access key is `sk-codex-proxy`; no token environment variable is required:

```sh
./run.sh serve
```

After binding the listener, the server prints client setup instructions: the API URL with the detected LAN IPv4 address, built-in key, a ready-to-copy `config.toml` snippet using the host's model, and the command to start Codex. The printed base URL, catalog URL, and download command all use that address.

The proxy always listens on `0.0.0.0` (all IPv4 interfaces), port `8787` by default. To change the port:

```sh
./run.sh serve --port 9000
```

Use an SSH tunnel, a private encrypted network, or an HTTPS reverse proxy across machines. Plain HTTP on a network exposes the proxy token and conversation. A token holder can consume the host account's model access and quota. The host's OpenAI/ChatGPT credentials are never sent to the client.

## Connect from the client

Copy the configuration printed by the host into the client's user-level `~/.codex/config.toml`. It contains the host's detected LAN address, so the client can connect directly.

For an optional SSH tunnel, run this on the **client** and leave it open:

```sh
ssh -N -L 8787:127.0.0.1:8787 user@HOST
```

On the host, generate the configuration snippet separately; it uses the host's selected model and detected LAN IP:

```sh
./run.sh client-config
```

Merge the output into the **client's user-level** `~/.codex/config.toml` (or `$CODEX_HOME/config.toml`). Put root keys before TOML tables, and replace existing `model` / `model_provider` keys instead of duplicating them. Example structure for a host at `192.168.1.10`:

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

For an SSH tunnel, generate the configuration with `./run.sh client-config --base-url http://127.0.0.1:8787/v1`; for HTTPS, supply your endpoint with `--base-url`. Keep the model name available to the host account. The client config contains the built-in key, so use Codex normally:

```sh
codex
```

No OpenAI login or host `auth.json` copy is needed on the client. Existing client sandbox and approval settings continue to apply.

## Missing model metadata

The model ID and its metadata are separate. A custom provider needs a Codex-native catalog for model selection, context limits, and reasoning options. The generated config now sets `model_catalog_url` to the proxy's authenticated `/v1/codex-models` endpoint. Restart the proxy and replace the client provider snippet with the newly generated one.

The endpoint exports the host's `model_catalog_json` when configured, otherwise the `models` array from `$CODEX_HOME/models_cache.json`. It does not export the cache's identity/account fields. Run Codex on the host to populate that cache if it is missing. Model IDs and capabilities are preserved.

If a client such as v0.153.2 still reports missing metadata or does not support `model_catalog_url`, download the catalog on the client using the command printed by the host. Example for a host at `192.168.1.10`:

```sh
mkdir -p "$HOME/.codex"
curl -fSs -H 'Authorization: Bearer sk-codex-proxy' \
  http://192.168.1.10:8787/v1/codex-models -o "$HOME/.codex/proxy-models.json"
```

Add this **root key before TOML tables** in the client's `config.toml`, using the downloaded file's absolute path, and restart Codex:

```toml
model_catalog_json = "/home/YOUR_USER/.codex/proxy-models.json"
```

This local catalog is a snapshot; download it again when the host models change. If the older client cannot parse newer model metadata, update it to match the host's Codex version. Metadata alone does not establish model access; a successful model response is still needed to verify inference. The bubblewrap warning concerns the client's command sandbox and is separate from model discovery.

## Options and behavior

```sh
# Host configuration in another directory
./run.sh --codex-home /path/to/.codex serve

# Host profile: <name>.config.toml or legacy [profiles.<name>]
./run.sh --profile work serve

# Override the generated client model
./run.sh client-config --model YOUR_MODEL --base-url https://proxy.example.com/v1

# Alternate Codex executable or upstream socket idle timeout (seconds)
./run.sh serve --codex-bin /path/to/codex --upstream-timeout 300

# Optional install; then use codex-proxy in place of ./run.sh
python3 -m pip install .
```

- Authenticated routes: `POST /v1/responses`, `POST /v1/responses/compact`, `GET /v1/models`, and `GET /v1/codex-models` (host metadata). The `/v1` prefix is optional. `GET /healthz` is an unauthenticated liveness check and does not test upstream access.
- SSE, tool calls/results, reasoning, and image payloads pass through without JSON rewriting. Gzip/deflate upstream responses are decoded, and SSE is forwarded with explicit HTTP chunk framing. Missing final SSE separators are repaired. Valid SSE with a missing or mislabeled content type is detected from the decoded body and forwarded as `text/event-stream`. Interrupted streams receive an explicit error event; the proxy never invents a successful completion. A streaming request receiving HTTP 200 with a non-SSE body returns 502. Other upstream status codes and error bodies pass through. Request bodies are limited to 64 MiB; HTTP streaming is supported, WebSockets are disabled in the client configuration.
- Credentials reload from `auth.json` on every request. For expired ChatGPT tokens or one upstream 401, the proxy asks `codex app-server` to refresh the host's file-based login, then retries once. Codex owns OAuth refresh and credential persistence. Concurrent refreshes within this proxy are serialized; avoid multiple proxy processes renewing the same login simultaneously.
- `config.toml` is read at startup; restart after configuration changes. Host provider headers override incoming headers. Client credentials, account IDs, cookies, API keys, and organization/project routing headers are removed. Redirects are not followed. Logs contain route names, statuses, byte/event counts, event types, encoding, request IDs, and error categories; tokens and conversation payloads are not logged.
- OS-keyring-only and ephemeral credentials are unsupported. This is a model API proxy: it does not expose the host's filesystem, execute host jobs, or proxy app-server, plugin, or account-management APIs. A ChatGPT `/models` response remains a Codex-native catalog, rather than a generic OpenAI model list.
- `HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY` and `CODEX_CA_CERTIFICATE` are respected for upstream connections. Use a TLS-terminating reverse proxy with response buffering disabled when exposing HTTPS.

## Stream disconnects

`HTTP 200` alone does not establish that a response finished. After a request, the host now logs whether the upstream sent a terminal event:

```text
INFO POST /responses -> 200 bytes=... events=... last=response.completed terminal=response.completed encoding=identity request_id=...
WARNING POST /responses -> 200 bytes=... events=... last=response.created terminal=missing encoding=identity request_id=...
```

Restart the host proxy after updating the program. Retry once from the client and inspect the new log. `terminal=missing` means the upstream ended without a valid terminal event; `response.failed` or `error` means the upstream reported failure. The client receives these failures instead of a false success. Connection exceptions also log their category, upstream status, encoding, content type, event count, last event, request ID, and an explicit `reason=...`.

For an early failure with `bytes=0 events=0`, inspect `reason=...`: an empty HTTP 200 response, an unsupported content encoding, a DNS failure, a TLS failure, and a refused connection have distinct messages. Missing content-type headers alone no longer cause valid SSE to be rejected. Tokens and response bodies are not included in those diagnostics.

If the host logs `terminal=response.completed` but the client still rejects the stream, include that log and both machines' `codex --version` when diagnosing client/protocol compatibility. The regression suite checks compressed responses, fragmented/CRLF events, truncated HTTP chunks, missing final separators, upstream failure events, and HTTP 200 non-SSE responses. A live upstream inference request remains necessary to establish that a particular deployment works.

## Verify

```sh
curl http://127.0.0.1:8787/healthz

curl -N http://127.0.0.1:8787/v1/responses \
  -H 'Authorization: Bearer sk-codex-proxy' \
  -H 'Content-Type: application/json' \
  -d '{"model":"YOUR_HOST_MODEL","instructions":"Be concise.","input":[{"role":"user","content":"Reply with OK"}],"stream":true,"store":false}'

python3.14 -m unittest discover -s tests -v

# In environments that disallow local network listeners:
CODEX_PROXY_TEST_TRANSPORT=socketpair python3.14 -m unittest discover -s tests -v
```

Use any Python 3.11+ executable for tests. Tests use local mock upstreams and temporary credentials; they do not spend model quota or modify the host login.

The configuration follows OpenAI's [custom provider configuration](https://developers.openai.com/codex/config-advanced), [authentication and credential storage](https://developers.openai.com/codex/auth), [gateway compatibility requirements](https://learn.chatgpt.com/docs/enterprise/gateway-compatibility), and [app-server account API](https://learn.chatgpt.com/docs/app-server).
