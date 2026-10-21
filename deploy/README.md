# Deployment

Nothing is built on the servers and no source code is copied there: they get only run files (compose files,
configuration, secrets, data). Everything except secrets is versioned here.

```
Browser ──https──> VPS 65.21.180.156: Caddy ──forward_auth──> Authelia (127.0.0.1:9091)
                      │ logged in: adds Remote-User / Remote-Groups / Remote-Email / Remote-Name
                      └──Tailscale──> tinkergw 100.126.3.52:8100
```

| Where | Folder | What |
|---|---|---|
| `deploy/edge/` → VPS `/opt/stocksvest/edge` | `docker-compose.yml`, `stocksvest.caddy`, `authelia/configuration.yml` | Authelia, and the Caddy site blocks for `stocksvest.hardansaeed.com` and `auth.hardansaeed.com`. The VPS Caddyfile gets one line: `import /opt/stocksvest/edge/stocksvest.caddy` |
| VPS only | `secrets/{jwt,session,storage}`, `authelia/users.yml`, `authelia/data/`, `authelia/notifications.txt` | Created on the server; never in git |
| `deploy/tinkergw/` → `tinkergw:~/stocksvest` | `docker-compose.yml` | Step 1: the `whoami` dummy app. Later the market service, the app and Grafana |

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
scripts/deploy-tinkergw.sh          # the apps on tinkergw
```

`deploy-edge.sh` validates the Authelia configuration and the Caddyfile before reloading Caddy, and backs up the
Caddyfile the first time it adds the import line. The other sites on the VPS are untouched.
