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
| `deploy/edge/` → VPS `/opt/stocksvest-market/edge` | `docker-compose.yml`, `stocksvest.caddy`, `authelia/configuration.yml` | Authelia, and the Caddy site blocks for `stocksvest.hardansaeed.com` and `auth.hardansaeed.com`. The VPS Caddyfile gets one line: `import /opt/stocksvest-market/edge/stocksvest.caddy` |
| VPS only | `secrets/{jwt,session,storage,smtp_password}`, `.env`, `authelia/users.yml`, `authelia/data/` | Created on the server; never in git |
| `deploy/tinkergw/` → `tinkergw:~/stocksvest` | `docker-compose.yml`, `backup.sh`, `grafana/` | The market service, the app and Grafana (image `stocksvest:<git commit>`, built on the Mac) |
| tinkergw only | `.env` (copied once from the Mac), `data/`, `backups/` | Secrets, the databases, nightly backups (03:15, 14 days) |

**On the VPS, everything of this project lives in `/opt/stocksvest-market/`.** `/opt/stocksvest/` belongs to the
other StocksVest project, whose `deploy.sh` wipes it (except `data/` and `secrets/`) on every deploy: never put
anything there.

## Backups

- **tinkergw:** every night at 03:15, `backup.sh` makes online copies of `app.db` and `market.db` into
  `~/stocksvest/backups/YYYY-MM-DD/` (kept 14 days) and pushes them to the VPS.
- **VPS:** `/opt/stocksvest-market/backups/app/` keeps the 30 newest days plus the first backup of each of the last 12
  months; `market/` keeps the 3 newest (it can be rebuilt from Alpaca). `prune-backups.py` thins them out daily at 05:30.
- tinkergw pushes with its own key (`~/.ssh/stocksvest_backup`), which the VPS accepts only for writing into that
  folder (`rrsync -wo`): no shell, no reading, no deleting. Set up once with `scripts/setup-offsite-backup.sh`.
- Restore: stop the stack on tinkergw, copy the files back into `~/stocksvest/data/` (gunzip `market.db.gz`), start it.

## Login

- Every request to `stocksvest.hardansaeed.com` needs a password **and** a second factor (passkey or TOTP).
- One session covers every `*.hardansaeed.com` site behind Authelia: 12 h at most, 2 h without activity, 1 month
  with "remember me".
- 3 wrong passwords within 2 minutes block the user for 15 minutes.
- One-time codes are emailed to the user's address (users.yml) from `StocksVest <alerts@stocksvest.net>` through
  Brevo (`smtp-relay.brevo.com:587`; `stocksvest.net` is authenticated there with DKIM). The SMTP login and key live
  only on the servers: `scripts/set-smtp-key.sh` asks for them and writes `edge/secrets/smtp_password` + `edge/.env`
  on the VPS and `SMTP_*` in tinkergw's `.env`.

## Commands

```bash
scripts/edge-set-password.sh        # create or change the login (asks on your terminal, sends only the hash)
scripts/set-smtp-key.sh             # Brevo SMTP login and key for Authelia and alerts (asks on your terminal)
scripts/deploy-edge.sh              # Authelia + Caddy on the VPS
scripts/deploy-tinkergw.sh          # tests, build linux/amd64, load on tinkergw, restart, health checks
STOCKSVEST_TAG=<tag> scripts/deploy-tinkergw.sh --no-build    # roll back to an image already on tinkergw
ssh saeed@tinkergw.local 'cd stocksvest && docker compose logs -f app'    # logs
```

`deploy-edge.sh` validates the Authelia configuration and the Caddyfile before reloading Caddy, and backs up the
Caddyfile the first time it adds the import line. The other sites on the VPS are untouched.
