# 🌐 Traefik Deployments Environment Secrets configuration
# This NixOS module manages secrets and environment variables for the unified apps in /traefik-deployments

{ config, lib, pkgs, ... }:

{
  # Define the keys to decrypt from secrets.yaml (avoiding repeating the owner boilerplate)
  sops.secrets = lib.genAttrs [
    "learning/turso_db_url"
    "learning/turso_auth_token"
    "mongo/root_username"
    "mongo/root_password"
    "ollama/api_key"
    "postgres/user"
    "postgres/password"
    "postgres/db"
    "gitea/runner_token"
  ] (name: { owner = "kiskaadee"; });

  # Generate the unified environment file at runtime in /run/secrets/traefik-deployments.env
  sops.templates."traefik-deployments.env" = {
    owner = "kiskaadee";
    content = lib.generators.toKeyValue {} {
      # --- Core / Infrastructure Settings ---
      PROXY_NETWORK = "proxy-net";
      CERT_RESOLVER = "myresolver";
      DOMAIN_SUFFIX = "roadtotech.me";

      # --- Dashboard & Landing ---
      DASHBOARD_DOMAIN = "dashboard.roadtotech.me";
      LANDING_DOMAIN = "roadtotech.me";

      # --- Docs ---
      DOCS_DOMAIN = "docs.roadtotech.me";
      DOCS_PROJECT_PATH = "/home/kiskaadee/Brain";

      # --- Excalidraw ---
      EXCALIDRAW_DOMAIN = "excalidraw.roadtotech.me";

      # --- Gitea ---
      GITEA_DOMAIN = "gitea.roadtotech.me";
      GITEA_SSH_DOMAIN = "gitea.roadtotech.me";
      GITEA_SSH_PORT = "2223";
      GITEA_AUTH_MIDDLEWARE = "https-redirect@docker";
      GITEA_RUNNER_TOKEN = config.sops.placeholder."gitea/runner_token";

      # --- Jellyfin ---
      JELLYFIN_SERVICE_NAME = "jellyfin";
      JELLYFIN_CONTAINER_NAME = "jellyfin";
      JELLYFIN_IMAGE_TAG = "latest";
      JELLYFIN_DOMAIN = "jellyfin.roadtotech.me";
      JELLYFIN_TZ = "UTC";
      JELLYFIN_PUID = "1000";
      JELLYFIN_PGID = "1000";
      JELLYFIN_MEDIA_PATH = "/media";
      JELLYFIN_HTTP_ENTRYPOINT = "web";
      JELLYFIN_HTTPS_ENTRYPOINT = "websecure";
      JELLYFIN_RATE_LIMIT_AVG = "100";
      JELLYFIN_RATE_LIMIT_BURST = "50";

      # --- Learning Dashboard ---
      LEARNING_SERVICE_NAME = "learning-hub";
      LEARNING_DOMAIN = "learning.roadtotech.me";
      LEARNING_APP_PORT = "8000";
      LEARNING_SOCKET_PROXY_NETWORK = "socket-net";
      LEARNING_DOCKER_HOST = "tcp://socket-proxy:2375";
      TURSO_DATABASE_URL = config.sops.placeholder."learning/turso_db_url";
      TURSO_AUTH_TOKEN = config.sops.placeholder."learning/turso_auth_token";

      # --- Mermaid ---
      MERMAID_DOMAIN = "mermaid.roadtotech.me";
      MERMAID_SERVICE_NAME = "mermaid";

      # --- Minecraft ---
      MINECRAFT_DOMAIN = "minecraft.roadtotech.me";

      # --- MongoDB ---
      MONGO_ROOT_USERNAME = config.sops.placeholder."mongo/root_username";
      MONGO_ROOT_PASSWORD = config.sops.placeholder."mongo/root_password";
      MONGO_DOMAIN = "mongodb.roadtotech.me";

      # --- PostgreSQL ---
      POSTGRES_USER = config.sops.placeholder."postgres/user";
      POSTGRES_PASSWORD = config.sops.placeholder."postgres/password";
      POSTGRES_DB = config.sops.placeholder."postgres/db";
      POSTGRES_DOMAIN = "pgsql.roadtotech.me";

      # --- Ollama ---
      OLLAMA_DOMAIN = "ollama.roadtotech.me";
      OLLAMA_API_KEY = config.sops.placeholder."ollama/api_key";
      OLLAMA_REPLICAS = "1";
      OLLAMA_DATA_VOLUME = "ollama_data";
    };
  };
}
