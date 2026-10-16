# Codex host proxy

Share a host machine's Codex model access with another machine. Commands, file edits, approvals, and MCP tools run on the client.

The host needs Python 3.11+ and Codex CLI. There are no Python runtime dependencies. The client needs Codex CLI.

## Start the host

```sh
python codex_proxy.py
```

The proxy reads the host's `~/.codex/config.toml` and `auth.json` (or `$CODEX_HOME`), listens on `0.0.0.0:8787`, and prints client setup with the detected LAN IP and host model.

If the host needs to log in, or its credentials are stored only in the OS keyring, first run:

```sh
codex -c 'cli_auth_credentials_store="file"' login
```

File-based ChatGPT and API-key logins and custom Responses providers are supported. Credentials reload on each request; expired ChatGPT logins renew automatically. Restart after changing host configuration.

The built-in API key is `sk-codex-proxy`, shared across installations. Use a trusted network, SSH tunnel, or HTTPS reverse proxy; plain HTTP does not encrypt conversations or the key.

## Connect the client

1. Merge the TOML printed by the host into the client's `~/.codex/config.toml` (or `$CODEX_HOME/config.toml`). Replace existing keys and put root keys before tables.
2. Start `codex` on the client.

The client needs no OpenAI login or copy of the host's credentials. Its sandbox and approval settings still apply.

The printed provider config includes `capabilities.remote_compaction = "v2"` to send compaction triggers through Responses. For an existing client config, add that line under `[model_providers.codex_proxy]` and restart Codex. If the host uses a custom upstream without v2 compaction support, set it to `"unsupported"` to use local compaction. See the [official configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).

For an SSH tunnel, run `ssh -N -L 8787:127.0.0.1:8787 user@HOST` on the client and use `http://127.0.0.1:8787/v1` as `base_url` and `http://127.0.0.1:8787/v1/codex-models` as `model_catalog_url`.

## Options

```sh
python codex_proxy.py --port 9000
python codex_proxy.py --codex-home /path/to/.codex --profile work
python codex_proxy.py --help
```

Also available: `--codex-bin /path/to/codex` and `--upstream-timeout 300` (seconds). Profiles support `<name>.config.toml` files and `[profiles.<name>]` tables.

## Troubleshooting

- **Connection fails:** check host reachability and `curl http://127.0.0.1:8787/healthz` on the host. This checks liveness; a client conversation verifies model access.
- **Old sessions fail:** try `/new` and send a short message. If that works, transfer a plain-text task summary; old history may contain incompatible encrypted state.
- **Missing model metadata:** use the printed `model_catalog_url` and fallback download instructions. Align client and host Codex versions if the catalog cannot be parsed.
- **Stream or compaction failures:** inspect the host log. `terminal=response.completed` means completion; `terminal=missing` means an incomplete stream. Upstream failures include error identifiers when available. Compare with a fresh conversation if only old sessions fail.
- **`response protection is unavailable` during compaction:** check that the client provider has `capabilities.remote_compaction = "v2"`, restart Codex, and retry. This selects the compaction protocol; it does not guarantee a fix for an upstream protection error. If it persists, inspect the host log and compare with a fresh conversation.

The proxy supports HTTP Responses streaming, compaction, and model lists; WebSockets are unsupported. Upstream connections respect `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`, and `CODEX_CA_CERTIFICATE`.

## Development

```sh
python -m unittest discover -s tests -v
```

Tests use temporary credentials and mock upstreams. If local listeners are restricted, prefix the command with `CODEX_PROXY_TEST_TRANSPORT=socketpair`.
