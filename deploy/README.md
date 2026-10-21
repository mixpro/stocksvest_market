# Deployment

Nothing is built on the servers and no source code is copied there: they get only run files (compose files,
configuration, secrets, data). Everything except secrets is versioned here.

```
Browser ──https──> VPS 65.21.180.156: Caddy ──forward_auth──> Authelia (127.0.0.1:9091)
                      │ logged in: adds Remote-User / Remote-Groups / Remote-Email / Remote-Name
                      ├─ stocksvest.hardansaeed.com  ──Tailscale──> tinkergw 100.126.3.52:8100  (app)
                      └─ grafanavest.hardansaeed.com ──Tailscale──> tinkergw 100.126.3.52:3000  (Grafana)
```

**Production runs only on tinkergw.** The Mac is for development: its `.env` has `RUN_SCHEDULER=false` and
`APP_RUN_SCHEDULER=false`, so a local `docker compose up` never syncs or trades on its own.

On tinkergw (`~/stocksvest`), every service uses the host network with an explicit address:

| Service | Listens on | Reached by |
|---|---|---|
| market | `127.0.0.1:8000` | the app on the same machine (and the IB Gateway on `127.0.0.1:4002` is next to it) |
| app | `100.126.3.52:8100` | the VPS. A `Remote-User` header counts only from the VPS (`APP_TRUSTED_PROXIES=100.96.27.63`); anyone else needs an API key |
| grafana | `100.126.3.52:3000` | the VPS (Grafana's auth proxy trusts `Remote-User` only from the VPS); the admin password works over Tailscale |

| Where | Folder | What |
|---|---|---|
| `deploy/edge/` → VPS `/opt/stocksvest/edge` | `docker-compose.yml`, `stocksvest.caddy`, `authelia/configuration.yml` | Authelia, and the Caddy site blocks for `stocksvest.hardansaeed.com` and `auth.hardansaeed.com`. The VPS Caddyfile gets one line: `import /opt/stocksvest/edge/stocksvest.caddy` |
| VPS only | `secrets/{jwt,session,storage}`, `authelia/users.yml`, `authelia/data/`, `authelia/notifications.txt` | Created on the server; never in git |
| `deploy/tinkergw/` → `tinkergw:~/stocksvest` | `docker-compose.yml`, `backup.sh`, `grafana/` | The market service, the app and Grafana (image `stocksvest:<git commit>`, built on the Mac) |
| tinkergw only | `.env` (copied once from the Mac), `data/`, `backups/` | Secrets, the databases, nightly backups (03:15, 14 days) |

## Login

- Every request to `stocksvest.hardansaeed.com` needs a password **and** a second factor (passkey or TOTP).
- One session covers every `*.hardansaeed.com` site behind Authelia: 12 h at most, 2 h without activity, 1 month
  with "remember me".
- 3 wrong passwords within 2 minutes block the user for 15 minutes.
- There's no mail server yet: Authelia writes its one-time codes to `notifications.txt` on the VPS:
  `ssh root@65.21.180.156 tail -n 20 /opt/stocksvest/edge/authelia/notifications.txt`

## Commands

```bash
scripts/edge-set-password.sh        # create or change the login (asks on your terminal, sends only the hash)
scripts/deploy-edge.sh              # Authelia + Caddy on the VPS
scripts/deploy-tinkergw.sh          # tests, build linux/amd64, load on tinkergw, restart, health checks
STOCKSVEST_TAG=<tag> scripts/deploy-tinkergw.sh --no-build    # roll back to an image already on tinkergw
ssh saeed@tinkergw.local 'cd stocksvest && docker compose logs -f app'    # logs
```

`deploy-edge.sh` validates the Authelia configuration and the Caddyfile before reloading Caddy, and backs up the
Caddyfile the first time it adds the import line. The other sites on the VPS are untouched.
