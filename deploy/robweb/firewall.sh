#!/usr/bin/env bash
# Host firewall for the robweb Desktop Web stack. Idempotent; run with sudo, then
# `netfilter-persistent save`. The preview relay has no login of its own (nginx's
# auth_request guards it), so only the server itself and the Desktop Web container
# (interface hdw0) may reach it. Any other container or LAN host is dropped.
set -euo pipefail
add() { iptables -C INPUT "$@" 2>/dev/null || iptables -I INPUT "$@"; }
# -I inserts at the top, so add DROP first, then the ACCEPTs above it.
add -p tcp --dport 18091 -j DROP
add -i hdw0 -p tcp --dport 18091 -j ACCEPT
add -i lo   -p tcp --dport 18091 -j ACCEPT
# Dashboard (:9119): Desktop Web container needs it; existing lo/docker0/br-+ rules stay.
iptables -C INPUT -i hdw0 -p tcp --dport 9119 -j ACCEPT 2>/dev/null || \
  iptables -I INPUT "$(iptables -S INPUT | grep -n -- '--dport 9119 -j DROP' | head -1 | cut -d: -f1 | awk '{print $1-1}')" -i hdw0 -p tcp --dport 9119 -j ACCEPT
iptables -S INPUT | grep -E 'dport (18091|9119)'
