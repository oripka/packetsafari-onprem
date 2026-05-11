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

  backend:
    image: "{{ backend_image }}"
    container_name: packetsafari-backend
    restart: always
    env_file:
      - "{{ runtime_env_path }}"
    environment:
      PACKETSAFARI_STORAGE_EXTERNAL_DIR: /storage
      PACKETSAFARI_RUNTIME_POSTGRES_ENABLED: "true"
      PACKETSAFARI_RUNTIME_ES_DISABLED: "true"
      PACKETSAFARI_CAPTURE_SHARKD_HOST: sharkd
      PACKETSAFARI_CAPTURE_SHARKD_PORT: "4448"
      PACKETSAFARI_SKIP_LEGACY_INDEX_BOOTSTRAP: "true"
      PACKETSAFARI_RUNTIME_CHECKPOINT_REDIS_DB: "0"
    ports:
      - "8080:80"
    volumes:
      - packetsafari-storage:/storage
      - "{{ host_runtime_root }}:{{ container_runtime_root }}"
    depends_on:
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
      PACKETSAFARI_SKIP_LEGACY_INDEX_BOOTSTRAP: "true"
      PACKETSAFARI_RUNTIME_CHECKPOINT_REDIS_DB: "0"
    volumes:
      - packetsafari-storage:/storage
      - "{{ host_runtime_root }}:{{ container_runtime_root }}"
    depends_on:
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
