#!/usr/bin/env bash
# Checks that this machine can reach download.geofabrik.de, and if it can't,
# prints what's needed to tell who is refusing the connection. Exits 0 if it
# can, 1 if it can't. The workflow runs it first in every region job.
#
# Background: since 2026-11-16 some jobs land on GitHub's AWS-hosted runners,
# and on every one so far all of Geofabrik's IPv4 addresses answered
# "Connection refused" about one round trip (~110 ms from us-east-1) after
# the attempt, while Azure-hosted runners connected fine. (Their IPv6
# addresses fail with "Network is unreachable" since the runners have no
# IPv6; that's expected and not the problem.) The diagnostics below are
# meant to show whether the refusal comes from Geofabrik's server, from
# Hetzner's network in front of it, or from somewhere on the AWS/GitHub side.
#
# Usage: ./check_geofabrik.sh [url]

set -uo pipefail

url=${1:-https://download.geofabrik.de/index-v1-nogeom.json}
host=$(printf '%s\n' "$url" | sed -E 's#^[a-z]+://([^/:]+).*#\1#')

if curl -sS --fail -o /dev/null -I --connect-timeout 10 --max-time 30 \
    --retry 3 --retry-delay 5 --retry-all-errors "$url"; then
  exit 0
fi

echo "::error::This runner can't reach $host; see the diagnostics below."

section() { printf '\n===== %s =====\n' "$*"; }

# One line per target: whether a TCP+TLS connection and an HTTP answer came
# back, and how long the connect took. Any HTTP status counts as reachable
# here; it's the connection that's in question. IPv4 only, since the runners
# have no IPv6. A plain GET rather than HEAD, because some servers (Hetzner's
# speed tests) don't answer HEAD; it only fetches the first byte.
# Usage: probe LABEL URL [extra curl args...]
probe() {
  local label=$1 target=$2 out
  shift 2
  out=$(curl -4 -sS -o /dev/null -r 0-0 --connect-timeout 10 --max-time 20 \
      -w 'http=%{http_code} ip=%{remote_ip} connect=%{time_connect}s\n' "$@" "$target" 2>&1)
  printf '%-28s %s\n' "$label" "$(printf '%s' "$out" | tr '\n' ' ')"
}

section "Runner"
grep -hE '^(PRETTY_NAME)=' /etc/os-release 2>/dev/null || true
printf 'RUNNER_ENVIRONMENT=%s ImageOS=%s\n' "${RUNNER_ENVIRONMENT:-}" "${ImageOS:-}"
printf 'Public IPv4 (checkip.amazonaws.com): '
curl -sS --max-time 10 https://checkip.amazonaws.com 2>&1 || echo
printf 'Public IPv4 (api.ipify.org): '
curl -sS --max-time 10 https://api.ipify.org 2>&1; echo

section "DNS"
getent ahosts "$host" || true

section "Comparison: who else can this runner reach?"
# Same server, other port and other Geofabrik hosts: is it one service or
# the whole machine/network refusing?
probe "geofabrik https (target)" "$url"
probe "geofabrik plain http :80" "http://$host/"
probe "www.geofabrik.de" "https://www.geofabrik.de/"
# Hetzner, same Helsinki data centre as Geofabrik (95.216/95.217 are
# Hetzner HEL1): if these are refused too, it's Hetzner's network, not
# Geofabrik's server.
probe "hetzner hel1 speedtest" "https://hel1-speed.hetzner.com/"
probe "hetzner fsn1 speedtest" "https://fsn1-speed.hetzner.com/"
probe "www.hetzner.com" "https://www.hetzner.com/"
# Unrelated, not on Hetzner: if these fail too, it's this runner's side.
probe "pypi.org" "https://pypi.org/simple/"
probe "www.google.com" "https://www.google.com/"
probe "github.com" "https://github.com/"

section "Every address of $host, IPv4 then IPv6"
for ip in $(getent ahostsv4 "$host" | awk '{print $1}' | sort -u); do
  probe "$ip" "$url" --resolve "$host:443:$ip"
done
curl -4 -v -o /dev/null -I --connect-timeout 10 --max-time 30 "$url" 2>&1 | grep -E '^\*[[:space:]]+(Trying|connect|Connected|Immediate|Failed)' || true
curl -6 -v -o /dev/null -I --connect-timeout 10 --max-time 30 "$url" 2>&1 | grep -E '^\*[[:space:]]+(Trying|connect|Connected|Immediate|Failed)' || true

first_ip=$(getent ahostsv4 "$host" | awk 'NR==1{print $1}')

# Where the refusal comes from: a TCP RST from the real server arrives with
# a TTL that has counted down over the whole route, one from a firewall
# near the runner with an almost untouched TTL. Compare with the hop count
# from traceroute below.
section "The refusal itself (tcpdump, RST packet TTL)"
if [ -n "$first_ip" ] && command -v tcpdump >/dev/null && sudo -n true 2>/dev/null; then
  dump_log=$(mktemp)
  sudo timeout 15 tcpdump -n -v -l -i any -c 4 "host $first_ip and tcp port 443" >"$dump_log" 2>&1 &
  dump_pid=$!
  # Only connect once tcpdump is actually capturing (up to 5 s), or the
  # SYN and the refusal can go by before it starts.
  for _ in $(seq 50); do
    grep -q "listening on" "$dump_log" && break
    sleep 0.1
  done
  curl -sS -o /dev/null -I --connect-timeout 5 --max-time 5 --resolve "$host:443:$first_ip" "$url" 2>&1 || true
  wait "$dump_pid" 2>/dev/null || true
  cat "$dump_log"
  rm -f "$dump_log"
else
  echo "(skipped: needs tcpdump and passwordless sudo)"
fi

section "Route to $first_ip on TCP port 443 (traceroute -T)"
if [ -n "$first_ip" ] && sudo -n true 2>/dev/null; then
  if ! command -v traceroute >/dev/null; then
    sudo timeout 60 apt-get install -y -qq traceroute >/dev/null 2>&1 ||
      { sudo timeout 90 apt-get update -qq >/dev/null 2>&1 &&
        sudo timeout 60 apt-get install -y -qq traceroute >/dev/null 2>&1; } || true
  fi
  if command -v traceroute >/dev/null; then
    sudo timeout 60 traceroute -n -T -p 443 -q 1 -w 2 -m 25 "$first_ip" 2>&1 || true
  else
    echo "(skipped: couldn't install traceroute)"
  fi
else
  echo "(skipped: needs passwordless sudo)"
fi

section "Interfaces and routes"
ip -4 route 2>/dev/null || true
ip -6 route 2>/dev/null || true
ip -brief addr 2>/dev/null || true

exit 1
