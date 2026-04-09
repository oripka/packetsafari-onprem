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
      - backend

  backend:
    image: "{{ backend_image }}"
    container_name: packetsafari-backend
    restart: always
    env_file:
      - "{{ runtime_env_path }}"
    ports:
      - "8080:80"
    volumes:
      - packetsafari-storage:/storage
      - "{{ host_runtime_root }}:{{ container_runtime_root }}"

  worker:
    image: "{{ worker_image }}"
    container_name: packetsafari-worker
    restart: always
    env_file:
      - "{{ runtime_env_path }}"
    volumes:
      - packetsafari-storage:/storage
      - "{{ host_runtime_root }}:{{ container_runtime_root }}"

  redis:
    image: "{{ redis_image }}"
    container_name: packetsafari-redis
    restart: always
    env_file:
      - "{{ runtime_env_path }}"

  es01:
    image: "{{ es_image }}"
    container_name: packetsafari-es01
    restart: always
    environment:
      - discovery.type=single-node
      - xpack.security.enabled=false
      - ES_JAVA_OPTS=-Xms2g -Xmx2g
    volumes:
      - packetsafari-esdata:/usr/share/elasticsearch/data

  securityworker:
    image: "{{ securityworker_image }}"
    container_name: packetsafari-securityworker
    restart: always
    env_file:
      - "{{ runtime_env_path }}"
    volumes:
      - packetsafari-storage:/storage
      - "{{ host_runtime_root }}:{{ container_runtime_root }}"

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
  packetsafari-esdata:
