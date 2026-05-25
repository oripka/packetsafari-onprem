#!/usr/bin/env bash
set -euo pipefail

CHAIN_NAME="PACKETSAFARI_EGRESS_ENFORCE"
ESNET_SUBNET="172.20.0.0/24"
PROXY_IP="172.20.0.2"
PROXY_EGRESS_IP="172.21.0.2"
PROXY_PORT="10000"
DNS_POLICY_IP="172.20.0.3"
SHUTTING_DOWN=0

MONITORED=(
  "backend:172.20.0.20"
  "worker:172.20.0.21"
)

NO_EGRESS=(
  "egress-dns:172.20.0.3"
  "postgres:172.20.0.10"
  "redis:172.20.0.11"
  "sharkd:172.20.0.23"
  "init:172.20.0.24"
  "frontend:172.20.0.30"
)

reject_rule() {
  local ip="$1"
  shift
  iptables -A "${CHAIN_NAME}" -s "${ip}/32" "$@" -j REJECT --reject-with icmp-port-unreachable
}

install_chain() {
  iptables -N "${CHAIN_NAME}" >/dev/null 2>&1 || true
  iptables -F "${CHAIN_NAME}"

  iptables -A "${CHAIN_NAME}" -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN
  iptables -A "${CHAIN_NAME}" -s "${PROXY_IP}/32" -j ACCEPT
  iptables -A "${CHAIN_NAME}" -s "${PROXY_EGRESS_IP}/32" -j ACCEPT

  for entry in "${MONITORED[@]}"; do
    ip="${entry##*:}"
    iptables -A "${CHAIN_NAME}" -s "${ip}/32" -d "${PROXY_IP}/32" -p tcp --dport "${PROXY_PORT}" -j RETURN
    iptables -A "${CHAIN_NAME}" -s "${ip}/32" -d "${DNS_POLICY_IP}/32" -p udp --dport 53 -j RETURN
    iptables -A "${CHAIN_NAME}" -s "${ip}/32" -d "${DNS_POLICY_IP}/32" -p tcp --dport 53 -j RETURN
    reject_rule "${ip}" -p udp --dport 53
    reject_rule "${ip}" -p tcp --dport 53
    reject_rule "${ip}" -d "${PROXY_IP}/32"
    iptables -A "${CHAIN_NAME}" -s "${ip}/32" -d "${ESNET_SUBNET}" -j RETURN
    reject_rule "${ip}"
  done

  for entry in "${NO_EGRESS[@]}"; do
    ip="${entry##*:}"
    iptables -A "${CHAIN_NAME}" -s "${ip}/32" -d "${DNS_POLICY_IP}/32" -p udp --dport 53 -j RETURN
    iptables -A "${CHAIN_NAME}" -s "${ip}/32" -d "${DNS_POLICY_IP}/32" -p tcp --dport 53 -j RETURN
    reject_rule "${ip}" -p udp --dport 53
    reject_rule "${ip}" -p tcp --dport 53
    reject_rule "${ip}" -d "${PROXY_IP}/32"
    iptables -A "${CHAIN_NAME}" -s "${ip}/32" -d "${ESNET_SUBNET}" -j RETURN
    reject_rule "${ip}"
  done

  iptables -A "${CHAIN_NAME}" -j RETURN

  if ! iptables -C DOCKER-USER -j "${CHAIN_NAME}" >/dev/null 2>&1; then
    iptables -I DOCKER-USER 1 -j "${CHAIN_NAME}"
  fi
}
install_chain

shutdown() {
  SHUTTING_DOWN=1
}

trap shutdown TERM INT

while [ "${SHUTTING_DOWN}" -eq 0 ]; do
  install_chain
  sleep 30 &
  wait "$!" || true
done
