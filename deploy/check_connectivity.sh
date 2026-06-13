#!/usr/bin/env bash
# WCA connectivity probe — run on the Mac Mini to see which endpoints are
# reachable on the CURRENT network/VPN. Tells you exactly what needs a VPN.
#
#   odds + polymarket  -> reachable from Bahrain natively (NO VPN needed)
#   telegram           -> blocked from Bahrain (needs a VPN to unblock)
#   uk sportsbooks     -> only needed for MANUAL betting (not this host)
#
# Usage:  bash deploy/check_connectivity.sh
set -u

probe() {  # name  host  [path]
  local name="$1" host="$2" path="${3:-/}"
  printf "%-22s " "$name"
  if ! nslookup "$host" >/dev/null 2>&1; then
    printf "DNS FAIL  (host won't resolve — blocked/needs VPN)\n"; return
  fi
  local code
  code=$(curl -s -o /dev/null -m 8 -w "%{http_code}" "https://${host}${path}" 2>/dev/null)
  if [ "$code" = "000" ]; then
    printf "DNS ok but NO CONNECT (firewalled — needs VPN)\n"
  else
    printf "OK  (HTTP %s)\n" "$code"
  fi
}

echo "=== WCA endpoint reachability on this network ==="
echo "-- no VPN needed (should be OK from Bahrain) --"
probe "Odds API"        "api.the-odds-api.com" "/v4/sports"
probe "Polymarket CLOB" "clob.polymarket.com"  "/"
probe "Polymarket Gamma" "gamma-api.polymarket.com" "/"
probe "GitHub"          "github.com"           "/"
echo "-- needs a (non-UK, non-US) VPN to unblock --"
probe "Telegram API"    "api.telegram.org"     "/"
echo
echo "Interpretation:"
echo "  * Telegram DNS FAIL  -> bot can't message you. Turn on a VPN (NOT UK/US — Polymarket blocks those)."
echo "  * Polymarket FAIL while on a VPN -> that VPN region is geo-blocked by PM; switch region or split-tunnel."
echo "  * Goal: a network where Odds + Polymarket + Telegram are ALL 'OK' at once."
