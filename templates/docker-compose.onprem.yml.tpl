services:
  frontend:
    image: "{{ frontend_image }}"
    container_name: packetsafari-frontend
    restart: always
    env_file:
      - "{{ runtime_env_path }}"
    ports:
      - "3000:3000"
    depends_on:
      backend:
        condition: service_started

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
    ports:
      - "8080:80"
    volumes:
      - packetsafari-storage:/storage
      - "{{ host_runtime_root }}:{{ container_runtime_root }}"
    depends_on:
      storage-init:
        condition: service_completed_successfully
      postgres:
        condition: service_healthy
      redis:
        condition: service_started
      sharkd:
        condition: service_started

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
    depends_on:
      storage-init:
        condition: service_completed_successfully
      postgres:
        condition: service_healthy
      redis:
        condition: service_started
      sharkd:
        condition: service_started

  postgres:
    image: "{{ postgres_image }}"
    container_name: packetsafari-postgres
    restart: always
    env_file:
      - "{{ runtime_env_path }}"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER:-packetsafari} -d $${POSTGRES_DB:-packetsafari}"]
      interval: 5s
      timeout: 5s
      retries: 20
    volumes:
      - packetsafari-postgres:/var/lib/postgresql/data

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
