services:
  egress-dns:
    image: "{{ egress_dns_image }}"
    container_name: packetsafari-egress-dns
    restart: always
    command:
      - -conf
      - /etc/coredns/Corefile
    volumes:
      - "{{ host_runtime_root }}/configuration/dns/Corefile:/etc/coredns/Corefile:ro"
      - "{{ host_runtime_root }}/configuration/dns/internal.hosts:/etc/coredns/internal.hosts:ro"
    networks:
      packetsafari:
        ipv4_address: 172.20.0.3

  egress-ironproxy:
    image: "{{ egress_ironproxy_image }}"
    container_name: packetsafari-egress-ironproxy
    restart: always
    env_file:
      - "{{ runtime_env_path }}"
    environment:
      PACKETSAFARI_EGRESS_PROFILE: production
      IRON_PROXY_CONFIG: /etc/iron-proxy/proxy.yaml
      IRON_PROXY_STATE_DIR: /var/lib/packetsafari/egress-proxy
      IRON_PROXY_LOG_DIR: /var/log/packetsafari/iron-proxy
    volumes:
      - "{{ host_runtime_root }}/configuration/iron-proxy/proxy.production.generated.yaml:/etc/iron-proxy/proxy.yaml:ro"
      - "{{ host_runtime_root }}/configuration/custom-ca:/usr/local/share/ca-certificates:ro"
      - packetsafari-egress-monitor:/var/log/packetsafari/iron-proxy
      - packetsafari-egress-proxy-certs:/var/lib/packetsafari/egress-proxy
    healthcheck:
      test: ["CMD-SHELL", "test -s /var/lib/packetsafari/egress-proxy/ca.crt && /bin/bash -lc 'exec 3<>/dev/tcp/127.0.0.1/10000'"]
      interval: 5s
      timeout: 5s
      retries: 20
    networks:
      packetsafari:
        ipv4_address: 172.20.0.2

  egress-firewall:
    image: "{{ egress_firewall_image }}"
    container_name: packetsafari-egress-firewall
    restart: always
    command:
      - /bin/bash
      - /usr/local/bin/run_egress_firewall.sh
    cap_drop:
      - ALL
    cap_add:
      - NET_ADMIN
    security_opt:
      - no-new-privileges:true
    network_mode: host
    depends_on:
      egress-ironproxy:
        condition: service_healthy
    volumes:
      - "{{ host_runtime_root }}/configuration/egress-firewall/run_egress_firewall.sh:/usr/local/bin/run_egress_firewall.sh:ro"

  frontend:
    image: "{{ frontend_image }}"
    container_name: packetsafari-frontend
    restart: always
    env_file:
      - "{{ runtime_env_path }}"
    environment:
      NUXT_PUBLIC_API_BASE: "${NUXT_PUBLIC_API_BASE:-/api/v2/}"
      NUXT_PUBLIC_SHARKD_WS_URL: "${NUXT_PUBLIC_SHARKD_WS_URL:-}"
    ports:
      - "3000:3000"
    depends_on:
      backend:
        condition: service_started
      sharkd:
        condition: service_started
    networks:
      packetsafari:
        ipv4_address: 172.20.0.30

  storage-init:
    image: "{{ backend_image }}"
    container_name: packetsafari-storage-init
    restart: "no"
    env_file:
      - "{{ runtime_env_path }}"
    environment:
      PACKETSAFARI_STORAGE_EXTERNAL_DIR: /storage
      PACKETSAFARI_STORAGE_SUBDIRS: "upload colorrules temporary avatars uploadchunk capture-agent anoncap analysis analysis/runtime onprem onprem/state onprem/env onprem/secrets"
    command:
      - /bin/bash
      - -lc
      - |
        set -euo pipefail
        /usr/local/bin/setvolumepermissions.sh /
    volumes:
      - packetsafari-storage:/storage
      - "{{ host_runtime_root }}:{{ container_runtime_root }}"
    depends_on:
      egress-dns:
        condition: service_started
    networks:
      packetsafari:
        ipv4_address: 172.20.0.24
    dns:
      - 172.20.0.3

  backend:
    image: "{{ backend_image }}"
    container_name: packetsafari-backend
    restart: always
    env_file:
      - "{{ runtime_env_path }}"
    environment:
      PYTHONPATH: /app
      PACKETSAFARI_STORAGE_EXTERNAL_DIR: /storage
      PACKETSAFARI_RUNTIME_POSTGRES_ENABLED: "true"
      PACKETSAFARI_RUNTIME_ES_DISABLED: "true"
      PACKETSAFARI_CAPTURE_SHARKD_HOST: sharkd
      PACKETSAFARI_CAPTURE_SHARKD_PORT: "4448"
      PACKETSAFARI_CAPTURE_SHARKD_PROTOCOL: ws
      PACKETSAFARI_SKIP_LEGACY_INDEX_BOOTSTRAP: "true"
      PACKETSAFARI_RUNTIME_TASK_QUEUE_REDIS_HOST: redis
      PACKETSAFARI_RUNTIME_TASK_QUEUE_REDIS_PORT: "6379"
      PACKETSAFARI_RUNTIME_TASK_QUEUE_REDIS_DB: "0"
      PACKETSAFARI_RUNTIME_CACHE_REDIS_HOST: redis
      PACKETSAFARI_RUNTIME_CACHE_REDIS_PORT: "6379"
      PACKETSAFARI_RUNTIME_CACHE_REDIS_DB: "0"
      PACKETSAFARI_RUNTIME_CHECKPOINT_REDIS_DB: "0"
      PACKETSAFARI_EGRESS_PROFILE: production
      PACKETSAFARI_EGRESS_PROXY_URL: "${PACKETSAFARI_EGRESS_PROXY_URL:-http://egress-ironproxy:10000}"
      HTTP_PROXY: "${PACKETSAFARI_EGRESS_PROXY_URL:-http://egress-ironproxy:10000}"
      HTTPS_PROXY: "${PACKETSAFARI_EGRESS_PROXY_URL:-http://egress-ironproxy:10000}"
      NO_PROXY: "${PACKETSAFARI_EGRESS_NO_PROXY:-localhost,127.0.0.1,::1,backend,worker,postgres,redis,sharkd,storage-init,egress-ironproxy,egress-dns,.svc.packetsafari.internal,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16}"
      SSL_CERT_FILE: /etc/packetsafari/egress-proxy/ca.crt
      REQUESTS_CA_BUNDLE: /etc/packetsafari/egress-proxy/ca.crt
      CURL_CA_BUNDLE: /etc/packetsafari/egress-proxy/ca.crt
    ports:
      - "8080:80"
    volumes:
      - packetsafari-storage:/storage
      - "{{ host_runtime_root }}:{{ container_runtime_root }}"
      - "{{ host_runtime_root }}/configuration/iron-proxy:/app/configuration/iron-proxy"
      - "{{ host_runtime_root }}/configuration/egress-allowlist.production.yaml:/app/configuration/egress-allowlist.production.yaml"
      - "{{ host_runtime_root }}/configuration/approved-ai-egress-hosts.json:/app/configuration/approved-ai-egress-hosts.json"
      - "{{ host_runtime_root }}/configuration/approved-identity-egress-hosts.json:/app/configuration/approved-identity-egress-hosts.json"
      - packetsafari-egress-proxy-certs:/etc/packetsafari/egress-proxy:ro
    depends_on:
      storage-init:
        condition: service_completed_successfully
      postgres:
        condition: service_healthy
      redis:
        condition: service_started
      sharkd:
        condition: service_started
      egress-dns:
        condition: service_started
      egress-ironproxy:
        condition: service_healthy
      egress-firewall:
        condition: service_started
    networks:
      packetsafari:
        ipv4_address: 172.20.0.20
    dns:
      - 172.20.0.3

  worker:
    image: "{{ worker_image }}"
    container_name: packetsafari-worker
    restart: always
    env_file:
      - "{{ runtime_env_path }}"
    environment:
      PACKETSAFARI_STORAGE_EXTERNAL_DIR: /storage
      PACKETSAFARI_RUNTIME_POSTGRES_ENABLED: "true"
      PACKETSAFARI_RUNTIME_ES_DISABLED: "true"
      PACKETSAFARI_CAPTURE_SHARKD_HOST: sharkd
      PACKETSAFARI_CAPTURE_SHARKD_PORT: "4448"
      PACKETSAFARI_CAPTURE_SHARKD_PROTOCOL: ws
      PACKETSAFARI_SKIP_LEGACY_INDEX_BOOTSTRAP: "true"
      PACKETSAFARI_RUNTIME_TASK_QUEUE_REDIS_HOST: redis
      PACKETSAFARI_RUNTIME_TASK_QUEUE_REDIS_PORT: "6379"
      PACKETSAFARI_RUNTIME_TASK_QUEUE_REDIS_DB: "0"
      PACKETSAFARI_RUNTIME_CACHE_REDIS_HOST: redis
      PACKETSAFARI_RUNTIME_CACHE_REDIS_PORT: "6379"
      PACKETSAFARI_RUNTIME_CACHE_REDIS_DB: "0"
      PACKETSAFARI_RUNTIME_CHECKPOINT_REDIS_DB: "0"
      CELERY_AICHAT_CONCURRENCY: "${CELERY_AICHAT_CONCURRENCY:-2}"
      CELERY_INDEX_CONCURRENCY: "${CELERY_INDEX_CONCURRENCY:-1}"
      CELERY_AICHAT_LOGLEVEL: "${CELERY_AICHAT_LOGLEVEL:-info}"
      CELERY_INDEX_LOGLEVEL: "${CELERY_INDEX_LOGLEVEL:-info}"
      PACKETSAFARI_EGRESS_PROFILE: production
      PACKETSAFARI_EGRESS_PROXY_URL: "${PACKETSAFARI_EGRESS_PROXY_URL:-http://egress-ironproxy:10000}"
      HTTP_PROXY: "${PACKETSAFARI_EGRESS_PROXY_URL:-http://egress-ironproxy:10000}"
      HTTPS_PROXY: "${PACKETSAFARI_EGRESS_PROXY_URL:-http://egress-ironproxy:10000}"
      NO_PROXY: "${PACKETSAFARI_EGRESS_NO_PROXY:-localhost,127.0.0.1,::1,backend,worker,postgres,redis,sharkd,storage-init,egress-ironproxy,egress-dns,.svc.packetsafari.internal,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16}"
      SSL_CERT_FILE: /etc/packetsafari/egress-proxy/ca.crt
      REQUESTS_CA_BUNDLE: /etc/packetsafari/egress-proxy/ca.crt
      CURL_CA_BUNDLE: /etc/packetsafari/egress-proxy/ca.crt
    command:
      - /bin/bash
      - -lc
      - |
        set -euo pipefail
        shutdown() {
          kill -TERM "$${AICHAT_PID:-}" "$${INDEX_PID:-}" 2>/dev/null || true
        }
        trap shutdown TERM INT

        celery -A packetsafari.celery_app worker \
          --loglevel="$${CELERY_AICHAT_LOGLEVEL:-info}" \
          --without-gossip --without-mingle \
          --concurrency="$${CELERY_AICHAT_CONCURRENCY:-2}" \
          --queues=aichat \
          --hostname=aichat@%h &
        AICHAT_PID=$$!

        celery -A packetsafari.celery_app worker \
          --loglevel="$${CELERY_INDEX_LOGLEVEL:-info}" \
          --pool=threads \
          --without-gossip --without-mingle \
          --concurrency="$${CELERY_INDEX_CONCURRENCY:-1}" \
          --queues=index \
          --hostname=index@%h &
        INDEX_PID=$$!

        wait -n "$${AICHAT_PID}" "$${INDEX_PID}"
        EXIT_CODE=$$?
        shutdown
        wait || true
        exit "$${EXIT_CODE}"
    volumes:
      - packetsafari-storage:/storage
      - "{{ host_runtime_root }}:{{ container_runtime_root }}"
      - "{{ host_runtime_root }}/configuration/iron-proxy:/app/configuration/iron-proxy"
      - "{{ host_runtime_root }}/configuration/egress-allowlist.production.yaml:/app/configuration/egress-allowlist.production.yaml"
      - "{{ host_runtime_root }}/configuration/approved-ai-egress-hosts.json:/app/configuration/approved-ai-egress-hosts.json"
      - "{{ host_runtime_root }}/configuration/approved-identity-egress-hosts.json:/app/configuration/approved-identity-egress-hosts.json"
      - packetsafari-egress-proxy-certs:/etc/packetsafari/egress-proxy:ro
    depends_on:
      storage-init:
        condition: service_completed_successfully
      postgres:
        condition: service_healthy
      redis:
        condition: service_started
      sharkd:
        condition: service_started
      egress-dns:
        condition: service_started
      egress-ironproxy:
        condition: service_healthy
      egress-firewall:
        condition: service_started
    networks:
      packetsafari:
        ipv4_address: 172.20.0.21
    dns:
      - 172.20.0.3

  postgres:
    image: "{{ postgres_image }}"
    container_name: packetsafari-postgres
    restart: always
    env_file:
      - "{{ runtime_env_path }}"
    environment:
      POSTGRES_HOST_AUTH_METHOD: scram-sha-256
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER:-packetsafari} -d $${POSTGRES_DB:-packetsafari}"]
      interval: 5s
      timeout: 5s
      retries: 20
    volumes:
      - packetsafari-postgres:/var/lib/postgresql/data
    networks:
      packetsafari:
        ipv4_address: 172.20.0.10
    dns:
      - 172.20.0.3

  redis:
    image: "{{ redis_image }}"
    container_name: packetsafari-redis
    restart: always
    env_file:
      - "{{ runtime_env_path }}"
    command: >-
      sh -ec 'REDIS_PASSWORD="$${REDIS_PASSWORD:-$${PACKETSAFARI_RUNTIME_TASK_QUEUE_REDIS_PASSWORD:-$${PACKETSAFARI_RUNTIME_CACHE_REDIS_PASSWORD:-}}}"; test -n "$$REDIS_PASSWORD"; exec /opt/redis-stack/bin/redis-server --dir /data --save 20 1 --loglevel warning --protected-mode no --requirepass "$$REDIS_PASSWORD" --loadmodule /opt/redis-stack/lib/rediscompat.so --loadmodule /opt/redis-stack/lib/redisearch.so MAXSEARCHRESULTS 10000 MAXAGGREGATERESULTS 10000 --loadmodule /opt/redis-stack/lib/rejson.so'
    volumes:
      - packetsafari-redis:/data
    networks:
      packetsafari:
        ipv4_address: 172.20.0.11
    dns:
      - 172.20.0.3

  sharkd:
    image: "{{ sharkd_image }}"
    container_name: packetsafari-sharkd
    restart: always
    env_file:
      - "{{ runtime_env_path }}"
    ports:
      - "4448:4448"
    volumes:
      - packetsafari-storage:/storage
      - "{{ host_runtime_root }}:{{ container_runtime_root }}"
    depends_on:
      storage-init:
        condition: service_completed_successfully
      egress-dns:
        condition: service_started
    networks:
      packetsafari:
        ipv4_address: 172.20.0.23
    dns:
      - 172.20.0.3

  audit-forwarder:
    image: "{{ vector_image }}"
    container_name: packetsafari-audit-forwarder
    restart: always
    profiles: ["logging"]
    env_file:
      - "{{ runtime_env_path }}"
    command: ["--config", "/etc/vector/vector.toml"]
    volumes:
      - "{{ host_runtime_root }}:{{ container_runtime_root }}"
      - /var/run/docker.sock:/var/run/docker.sock

volumes:
  packetsafari-storage:
  packetsafari-postgres:
  packetsafari-redis:
  packetsafari-egress-proxy-certs:
  packetsafari-egress-monitor:

networks:
  packetsafari:
    driver: bridge
    ipam:
      config:
        - subnet: 172.20.0.0/24
