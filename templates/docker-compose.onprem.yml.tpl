x-packetsafari-journald-logging: &packetsafari-journald-logging
  driver: journald
  options:
    tag: "packetsafari/{{.Name}}/{{.ID}}"

services:
  egress-dns:
    image: "{{ egress_dns_image }}"
    container_name: packetsafari-egress-dns
    restart: always
    logging: *packetsafari-journald-logging
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
    logging: *packetsafari-journald-logging
    env_file:
      - "{{ ironproxy_env_path }}"
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
    logging: *packetsafari-journald-logging
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
    stop_grace_period: 3s
    depends_on:
      egress-ironproxy:
        condition: service_healthy
    volumes:
      - "{{ host_runtime_root }}/configuration/egress-firewall/run_egress_firewall.sh:/usr/local/bin/run_egress_firewall.sh:ro"

  frontend:
    image: "{{ frontend_image }}"
    container_name: packetsafari-frontend
    restart: always
    logging: *packetsafari-journald-logging
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
      agent-stream-gateway:
        condition: service_healthy
      sharkd:
        condition: service_started
    networks:
      packetsafari:
        ipv4_address: 172.20.0.30

  storage-init:
    image: "{{ backend_image }}"
    container_name: packetsafari-storage-init
    user: "0:0"
    restart: "no"
    logging: *packetsafari-journald-logging
    env_file:
      - "{{ runtime_env_path }}"
    environment:
      PACKETSAFARI_STORAGE_EXTERNAL_DIR: /storage
      PACKETSAFARI_STORAGE_SUBDIRS: "upload upload/archive colorrules temporary avatars uploadchunk capture-agent anoncap agent-visual-reports admin intelligence intelligence/ja4 intelligence/suricata intelligence/suricata/rules intelligence/suricata/update intelligence/zeek intelligence/zeek/intel runtime runtime/sharkd-ids-cache logs analysis analysis/runtime analysis/runtime/typed analysis/runtime/typed-securityscan analysis/runtime/typed-shared analysis/runtime/match-bitsets onprem onprem/state onprem/env onprem/secrets"
      PACKETSAFARI_STORAGE_REPAIR_SUBDIRS: "uploadchunk upload/archive capture-agent anoncap agent-visual-reports runtime/sharkd-ids-cache analysis/runtime/typed analysis/runtime/typed-securityscan analysis/runtime/typed-shared analysis/runtime/match-bitsets"
    command:
      - /bin/bash
      - -lc
      - |
        set -euo pipefail
        /usr/local/bin/setvolumepermissions.sh /
        PACKETSAFARI_STORAGE_EXTERNAL_DIR=/var/lib/packetsafari/codex PACKETSAFARI_STORAGE_SUBDIRS="sqlite" PACKETSAFARI_STORAGE_REPAIR_SUBDIRS="." /usr/local/bin/setvolumepermissions.sh /
    volumes:
      - packetsafari-storage:/storage
      - packetsafari-codexruntime:/var/lib/packetsafari/codex
      - "{{ host_runtime_root }}:{{ container_runtime_root }}"
    depends_on:
      egress-dns:
        condition: service_started
    networks:
      packetsafari:
        ipv4_address: 172.20.0.24
    dns:
      - 172.20.0.3

  agent-stream-gateway:
    image: "{{ backend_image }}"
    container_name: packetsafari-agent-stream-gateway
    user: backendu
    init: true
    restart: always
    stop_grace_period: 40s
    logging: *packetsafari-journald-logging
    env_file:
      - "{{ runtime_env_path }}"
    environment:
      PYTHONPATH: /app
      PACKETSAFARI_RUNTIME_ROLE: agent_stream_gateway
      PACKETSAFARI_SKIP_SERVICE_INIT: "true"
      PACKETSAFARI_RUNTIME_CACHE_REDIS_HOST: redis
      PACKETSAFARI_RUNTIME_CACHE_REDIS_PORT: "6379"
      PACKETSAFARI_RUNTIME_CACHE_REDIS_DB: "0"
      AI_AGENT_STREAM_TICKET_SECRET: "${AI_AGENT_STREAM_TICKET_SECRET:?required}"
      AI_AGENT_STREAM_TICKET_TTL_SECONDS: "${AI_AGENT_STREAM_TICKET_TTL_SECONDS:-30}"
    command:
      - uvicorn
      - packetsafari.agent_stream_gateway:app
      - --host
      - 0.0.0.0
      - --port
      - "8091"
      - --workers
      - "1"
      - --timeout-graceful-shutdown
      - "30"
      - --no-access-log
    expose:
      - "8091"
    healthcheck:
      test:
        [
          "CMD-SHELL",
          "python3 -c \"import http.client,sys; c=http.client.HTTPConnection('127.0.0.1',8091,timeout=3); c.request('GET','/healthz'); r=c.getresponse(); r.read(1024); sys.exit(0 if 200 <= r.status < 300 else 1)\""
        ]
      interval: 10s
      timeout: 5s
      retries: 6
      start_period: 20s
    depends_on:
      redis:
        condition: service_started
    networks:
      packetsafari:
        ipv4_address: 172.20.0.22
    dns:
      - 172.20.0.3

  backend:
    image: "{{ backend_image }}"
    container_name: packetsafari-backend
    restart: always
    logging: *packetsafari-journald-logging
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
      SHARKD_IDS_SHARED_CACHE_DIR: /storage/runtime/sharkd-ids-cache
      PACKETSAFARI_SKIP_LEGACY_INDEX_BOOTSTRAP: "true"
      PACKETSAFARI_RUNTIME_TASK_QUEUE_REDIS_HOST: redis
      PACKETSAFARI_RUNTIME_TASK_QUEUE_REDIS_PORT: "6379"
      PACKETSAFARI_RUNTIME_TASK_QUEUE_REDIS_DB: "0"
      PACKETSAFARI_RUNTIME_CACHE_REDIS_HOST: redis
      PACKETSAFARI_RUNTIME_CACHE_REDIS_PORT: "6379"
      PACKETSAFARI_RUNTIME_CACHE_REDIS_DB: "0"
      PACKETSAFARI_RUNTIME_CHECKPOINT_REDIS_DB: "0"
      AI_AGENT_STREAM_GATEWAY_INTERNAL_URL: "${AI_AGENT_STREAM_GATEWAY_INTERNAL_URL:-http://agent-stream-gateway:8091}"
      AI_AGENT_STREAM_TICKET_SECRET: "${AI_AGENT_STREAM_TICKET_SECRET:?required}"
      AI_AGENT_STREAM_TICKET_TTL_SECONDS: "${AI_AGENT_STREAM_TICKET_TTL_SECONDS:-30}"
      APP_BASE_URL: "${APP_BASE_URL:-https://packetsafari.com}"
      NEWSLETTER_FROM_EMAIL: "${NEWSLETTER_FROM_EMAIL:-contact@packetsafari.com}"
      NEWSLETTER_FROM_NAME: "${NEWSLETTER_FROM_NAME:-PacketSafari}"
      AWS_ACCESS_KEY_ID: "${PACKETSAFARI_PROXY_TOKEN_AWS_ACCESS_KEY_ID:-ps_proxy_aws_access_key_id}"
      AWS_SECRET_ACCESS_KEY: "${PACKETSAFARI_PROXY_TOKEN_AWS_SECRET_ACCESS_KEY:-ps_proxy_aws_secret_access_key}"
      PACKETSAFARI_EGRESS_PROFILE: production
      PACKETSAFARI_EGRESS_PROXY_URL: "${PACKETSAFARI_EGRESS_PROXY_URL:-http://egress-ironproxy:10000}"
      HTTP_PROXY: "${PACKETSAFARI_EGRESS_PROXY_URL:-http://egress-ironproxy:10000}"
      HTTPS_PROXY: "${PACKETSAFARI_EGRESS_PROXY_URL:-http://egress-ironproxy:10000}"
      NO_PROXY: "${PACKETSAFARI_EGRESS_NO_PROXY:-localhost,127.0.0.1,::1,backend,agent-stream-gateway,worker,postgres,redis,sharkd,storage-init,egress-ironproxy,egress-dns,.svc.packetsafari.internal,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16}"
      SSL_CERT_FILE: /etc/packetsafari/egress-proxy/ca.crt
      REQUESTS_CA_BUNDLE: /etc/packetsafari/egress-proxy/ca.crt
      CURL_CA_BUNDLE: /etc/packetsafari/egress-proxy/ca.crt
    ports:
      - "8080:80"
    healthcheck:
      test:
        [
          "CMD-SHELL",
          "python3 - <<'PY'\nimport http.client, sys\nconn = http.client.HTTPConnection('127.0.0.1', 80, timeout=3)\nconn.request('GET', '/api/v2/system/live', headers={'Connection': 'close'})\nresp = conn.getresponse()\nresp.read(1024)\nsys.exit(0 if 200 <= resp.status < 300 else 1)\nPY"
        ]
      interval: 15s
      timeout: 5s
      retries: 3
      start_period: 30s
    volumes:
      - packetsafari-storage:/storage
      - packetsafari-codexruntime:/var/lib/packetsafari/codex
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
      agent-stream-gateway:
        condition: service_healthy
    networks:
      packetsafari:
        ipv4_address: 172.20.0.20
    dns:
      - 172.20.0.3

  worker:
    image: "{{ worker_image }}"
    container_name: packetsafari-worker
    restart: always
    logging: *packetsafari-journald-logging
    env_file:
      - "{{ runtime_env_path }}"
    environment:
      PACKETSAFARI_STORAGE_EXTERNAL_DIR: /storage
      PACKETSAFARI_RUNTIME_POSTGRES_ENABLED: "true"
      PACKETSAFARI_RUNTIME_ES_DISABLED: "true"
      PACKETSAFARI_CAPTURE_SHARKD_HOST: sharkd
      PACKETSAFARI_CAPTURE_SHARKD_PORT: "4448"
      PACKETSAFARI_CAPTURE_SHARKD_PROTOCOL: ws
      SHARKD_IDS_SHARED_CACHE_DIR: /storage/runtime/sharkd-ids-cache
      PACKETSAFARI_SKIP_LEGACY_INDEX_BOOTSTRAP: "true"
      PACKETSAFARI_RUNTIME_TASK_QUEUE_REDIS_HOST: redis
      PACKETSAFARI_RUNTIME_TASK_QUEUE_REDIS_PORT: "6379"
      PACKETSAFARI_RUNTIME_TASK_QUEUE_REDIS_DB: "0"
      PACKETSAFARI_RUNTIME_CACHE_REDIS_HOST: redis
      PACKETSAFARI_RUNTIME_CACHE_REDIS_PORT: "6379"
      PACKETSAFARI_RUNTIME_CACHE_REDIS_DB: "0"
      PACKETSAFARI_RUNTIME_CHECKPOINT_REDIS_DB: "0"
      CELERY_AICHAT_CONCURRENCY: "${CELERY_AICHAT_CONCURRENCY:-2}"
      CELERY_INDEX_CONCURRENCY: "${CELERY_INDEX_CONCURRENCY:-auto}"
      CELERY_AICHAT_LOGLEVEL: "${CELERY_AICHAT_LOGLEVEL:-info}"
      CELERY_INDEX_LOGLEVEL: "${CELERY_INDEX_LOGLEVEL:-info}"
      APP_BASE_URL: "${APP_BASE_URL:-https://packetsafari.com}"
      NEWSLETTER_FROM_EMAIL: "${NEWSLETTER_FROM_EMAIL:-contact@packetsafari.com}"
      NEWSLETTER_FROM_NAME: "${NEWSLETTER_FROM_NAME:-PacketSafari}"
      AWS_ACCESS_KEY_ID: "${PACKETSAFARI_PROXY_TOKEN_AWS_ACCESS_KEY_ID:-ps_proxy_aws_access_key_id}"
      AWS_SECRET_ACCESS_KEY: "${PACKETSAFARI_PROXY_TOKEN_AWS_SECRET_ACCESS_KEY:-ps_proxy_aws_secret_access_key}"
      PACKETSAFARI_EGRESS_PROFILE: production
      PACKETSAFARI_EGRESS_PROXY_URL: "${PACKETSAFARI_EGRESS_PROXY_URL:-http://egress-ironproxy:10000}"
      HTTP_PROXY: "${PACKETSAFARI_EGRESS_PROXY_URL:-http://egress-ironproxy:10000}"
      HTTPS_PROXY: "${PACKETSAFARI_EGRESS_PROXY_URL:-http://egress-ironproxy:10000}"
      NO_PROXY: "${PACKETSAFARI_EGRESS_NO_PROXY:-localhost,127.0.0.1,::1,backend,agent-stream-gateway,worker,postgres,redis,sharkd,storage-init,egress-ironproxy,egress-dns,.svc.packetsafari.internal,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16}"
      SSL_CERT_FILE: /etc/packetsafari/egress-proxy/ca.crt
      REQUESTS_CA_BUNDLE: /etc/packetsafari/egress-proxy/ca.crt
      CURL_CA_BUNDLE: /etc/packetsafari/egress-proxy/ca.crt
    command:
      - /bin/bash
      - -lc
      - |
        set -euo pipefail
        PIDS=()
        shutdown() { kill -TERM "$${PIDS[@]}" 2>/dev/null || true; }
        trap shutdown TERM INT
        python3 /app/scripts/wait_for_backend_startup.py \
          --timeout-seconds "$${PACKETSAFARI_WORKER_BACKEND_STARTUP_WAIT_SECONDS:-45}"
        CELERY_AICHAT_CONCURRENCY="$$(python3 /app/scripts/resolve_worker_concurrency.py aichat)"
        CELERY_INDEX_CONCURRENCY="$$(python3 /app/scripts/resolve_worker_concurrency.py index)"
        export CELERY_AICHAT_CONCURRENCY CELERY_INDEX_CONCURRENCY
        echo "Resolved Celery worker concurrency: aichat=$$CELERY_AICHAT_CONCURRENCY index=$$CELERY_INDEX_CONCURRENCY"

        PACKETSAFARI_CELERY_TASK_PROFILE=aichat celery -A packetsafari.celery_app worker \
          --loglevel="$${CELERY_AICHAT_LOGLEVEL:-info}" \
          --without-gossip --without-mingle \
          --concurrency="$${CELERY_AICHAT_CONCURRENCY:-2}" \
          --queues=aichat,aichat_priority \
          --hostname=aichat@%h &
        AICHAT_PID=$$!
        PIDS+=("$$AICHAT_PID")

        CELERY_PRIORITY_AGENT_CONCURRENCY="$${PACKETSAFARI_CELERY_PRIORITY_AGENT_CONCURRENCY:-1}"
        PACKETSAFARI_CELERY_TASK_PROFILE=aichat celery -A packetsafari.celery_app worker \
          --loglevel="$${CELERY_AICHAT_LOGLEVEL:-info}" \
          --without-gossip --without-mingle --prefetch-multiplier=1 \
          --concurrency="$$CELERY_PRIORITY_AGENT_CONCURRENCY" \
          --queues=aichat_priority --hostname=aichat-priority@%h &
        PIDS+=("$$!")

        PACKETSAFARI_CELERY_TASK_PROFILE=index PACKETSAFARI_CELERY_RUN_STARTUP_MAINTENANCE=1 celery -A packetsafari.celery_app worker \
          --loglevel="$${CELERY_INDEX_LOGLEVEL:-info}" \
          --pool=threads \
          --without-gossip --without-mingle \
          --concurrency="$${CELERY_INDEX_CONCURRENCY:-auto}" \
          --queues=index,index_priority \
          --hostname=index@%h &
        INDEX_PID=$$!
        PIDS+=("$$INDEX_PID")

        CELERY_PRIORITY_ANALYSIS_CONCURRENCY="$${PACKETSAFARI_CELERY_PRIORITY_ANALYSIS_CONCURRENCY:-1}"
        PACKETSAFARI_CELERY_TASK_PROFILE=index_priority celery -A packetsafari.celery_app worker \
          --loglevel="$${CELERY_INDEX_LOGLEVEL:-info}" --pool=threads \
          --without-gossip --without-mingle --prefetch-multiplier=1 \
          --concurrency="$$CELERY_PRIORITY_ANALYSIS_CONCURRENCY" \
          --queues=index_priority --hostname=index-priority@%h &
        PIDS+=("$$!")

        # Standard Security is one bounded Sharkd IDS request on a dedicated
        # durable queue, isolated from index-worker capacity and prefetch.
        PACKETSAFARI_CELERY_TASK_PROFILE=security celery -A packetsafari.celery_app worker \
          --loglevel="$${CELERY_INDEX_LOGLEVEL:-info}" --pool=solo \
          --without-gossip --without-mingle --prefetch-multiplier=1 \
          --concurrency=1 --queues=security --hostname=security@%h &
        PIDS+=("$$!")

        CELERY_RESERVED_ANALYSIS_CONCURRENCY="$${PACKETSAFARI_RESERVED_ANALYSIS_SLOTS:-0}"
        if [ "$$CELERY_RESERVED_ANALYSIS_CONCURRENCY" -gt 0 ]; then
          PACKETSAFARI_CELERY_TASK_PROFILE=aichat celery -A packetsafari.celery_app worker \
            --loglevel="$${CELERY_AICHAT_LOGLEVEL:-info}" \
            --without-gossip --without-mingle --prefetch-multiplier=1 \
            --concurrency="$$CELERY_RESERVED_ANALYSIS_CONCURRENCY" \
            --queues=aichat_reserved --hostname=aichat-reserved@%h &
          PIDS+=("$$!")

          PACKETSAFARI_CELERY_TASK_PROFILE=index_priority celery -A packetsafari.celery_app worker \
            --loglevel="$${CELERY_INDEX_LOGLEVEL:-info}" --pool=threads \
            --without-gossip --without-mingle --prefetch-multiplier=1 \
            --concurrency="$$CELERY_RESERVED_ANALYSIS_CONCURRENCY" \
            --queues=index_reserved --hostname=index-reserved@%h &
          PIDS+=("$$!")
        fi

        wait -n "$${PIDS[@]}"
        EXIT_CODE=$$?
        shutdown
        wait || true
        exit "$${EXIT_CODE}"
    volumes:
      - packetsafari-storage:/storage
      - packetsafari-codexruntime:/var/lib/packetsafari/codex
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
    logging: *packetsafari-journald-logging
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
    logging: *packetsafari-journald-logging
    env_file:
      - "{{ runtime_env_path }}"
    command: >-
      sh -ec 'REDIS_PASSWORD="$${REDIS_PASSWORD:-$${PACKETSAFARI_RUNTIME_TASK_QUEUE_REDIS_PASSWORD:-$${PACKETSAFARI_RUNTIME_CACHE_REDIS_PASSWORD:-}}}"; test -n "$$REDIS_PASSWORD"; exec /opt/redis-stack/bin/redis-server --dir /data --save "3600 1 300 100 60 10000" --loglevel warning --protected-mode no --requirepass "$$REDIS_PASSWORD" --loadmodule /opt/redis-stack/lib/rediscompat.so --loadmodule /opt/redis-stack/lib/redisearch.so MAXSEARCHRESULTS 10000 MAXAGGREGATERESULTS 10000 --loadmodule /opt/redis-stack/lib/rejson.so'
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
    logging: *packetsafari-journald-logging
    stop_grace_period: 5s
    env_file:
      - "{{ runtime_env_path }}"
    environment:
      SHARKD_JWT_SECRET: "${SHARKD_JWT_SECRET:?required}"
      SHARKD_IDS_CACHE_DIR: /storage/runtime/sharkd-ids-cache
      SHARKD_IDS_SHARED_CACHE_DIR: /storage/runtime/sharkd-ids-cache
      SHARKD_IDS_ARTIFACT_ROOT: /storage/analysis/runtime/ids-alerts
    ports:
      - "4448:4448"
    healthcheck:
      test: ["CMD-SHELL", "/bin/bash -lc 'exec 3<>/dev/tcp/127.0.0.1/4448'"]
      interval: 5s
      timeout: 5s
      retries: 20
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
    logging: *packetsafari-journald-logging
    profiles: ["logging"]
    env_file:
      - "{{ runtime_env_path }}"
    command: ["--config", "/etc/vector/vector.toml"]
    volumes:
      - "{{ host_runtime_root }}:{{ container_runtime_root }}"
      - /var/run/docker.sock:/var/run/docker.sock

volumes:
  packetsafari-storage:
  packetsafari-codexruntime:
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
