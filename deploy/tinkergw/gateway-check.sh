#!/usr/bin/env bash
# Daily IB Gateway support check (cron on tinkergw, installed by scripts/deploy-tinkergw.sh).
# Runs stocksvest_market.gateway_check inside the market container (it has ib_async, the gateway on 127.0.0.1:4002
# and the SMTP settings from .env); emails 21, 7 and 2 days before IBKR's desupport date.
#   gateway-check.sh [--dry-run | --test-email]
set -euo pipefail
INSTALLED="$(grep -m1 '^TWS_MAJOR_VRSN=' "$HOME/ibc/gatewaystart.sh" 2>/dev/null | cut -d= -f2 || true)"
exec docker exec -e GATEWAY_VERSION="$INSTALLED" stocksvest-market python -m stocksvest_market.gateway_check "$@"
