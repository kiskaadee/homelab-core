# 🌐 Homeserver Core Environment Secrets & Systemd service configuration
# This NixOS module manages secrets and the systemd lifecycle for the core homeserver stack.

{ config, lib, pkgs, ... }:

{
  # Define the keys to decrypt from secrets.yaml
  sops.secrets = lib.genAttrs [
    "dynu/api_key"
    "traefik/acme_email"
    "authelia/session_secret"
    "authelia/storage_encryption_key"
    "authelia/jwt_secret"
    "lldap/jwt_secret"
    "lldap/key_seed"
    "lldap/admin_password"
    "gitops/webhook_secret"
    "brevo/smtp_login"
    "brevo/smtp_key"
    "diun/smtp_password"
  ] (name: { owner = "kiskaadee"; });

  # Generate the unified environment file at runtime in /run/secrets/homeserver.env
  sops.templates."homeserver.env" = {
    owner = "kiskaadee";
    content = lib.generators.toKeyValue {} {
      DOMAIN = "roadtotech.me";
      DOCKER_API_VERSION = "1.40";
      DYNU_API_KEY = config.sops.placeholder."dynu/api_key";
      ACME_EMAIL = config.sops.placeholder."traefik/acme_email";
      AUTHELIA_SESSION_SECRET = config.sops.placeholder."authelia/session_secret";
      AUTHELIA_STORAGE_ENCRYPTION_KEY = config.sops.placeholder."authelia/storage_encryption_key";
      AUTHELIA_IDENTITY_VALIDATION_RESET_PASSWORD_JWT_SECRET = config.sops.placeholder."authelia/jwt_secret";
      LLDAP_JWT_SECRET = config.sops.placeholder."lldap/jwt_secret";
      LLDAP_KEY_SEED = config.sops.placeholder."lldap/key_seed";
      LLDAP_LDAP_USER_PASS = config.sops.placeholder."lldap/admin_password";
      AUTHELIA_LDAP_PASSWORD = config.sops.placeholder."lldap/admin_password";
      SMTP_SERVER = "smtp-relay.brevo.com";
      SMTP_PORT = "587";
      SMTP_LOGIN = config.sops.placeholder."brevo/smtp_login";
      SMTP_KEY = config.sops.placeholder."brevo/smtp_key";
      DIUN_SMTP_PASSWORD = config.sops.placeholder."diun/smtp_password";
    };
  };

  # Define the declarative systemd service to manage the homeserver container stack
  systemd.services.homeserver-core = {
    description = "Homeserver Core Stack (Traefik, Authelia, Homepage, etc.)";
    after = [ "network-online.target" "docker.service" ];
    wants = [ "network-online.target" ];
    wantedBy = [ "multi-user.target" ];

    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      WorkingDirectory = "/home/kiskaadee/Core";
      ExecStart = "${pkgs.docker-compose}/bin/docker-compose --env-file ${config.sops.templates."homeserver.env".path} up -d --remove-orphans";
      ExecStop = "${pkgs.docker-compose}/bin/docker-compose down";
    };
  };

  # Declarative Dynamic GitOps Webhook Service (Internal Port 9000)
  systemd.services.homelab-gitops = {
    description = "Dynamic Homelab GitOps Webhook Dispatcher";
    after = [ "network-online.target" "docker.service" ];
    wants = [ "network-online.target" ];
    wantedBy = [ "multi-user.target" ];

    path = with pkgs; [ git docker docker-compose python3 coreutils bash openssh ];

    environment = {
      GITOPS_SECRET_FILE = config.sops.secrets."gitops/webhook_secret".path;
    };

    serviceConfig = {
      Type = "simple";
      User = "kiskaadee";
      WorkingDirectory = "/home/kiskaadee/Core";
      ExecStart = "${pkgs.webhook}/bin/webhook -hooks ${pkgs.writeText "hooks.json" (builtins.toJSON [
        {
          id = "deploy";
          execute-command = "/home/kiskaadee/Core/scripts/gitops_dispatcher.py";
          pass-arguments-to-command = [
            { source = "raw-request-body"; }
            { source = "header"; name = "X-Gitea-Signature"; }
            { source = "header"; name = "X-Gitea-Event"; }
          ];
          command-working-directory = "/home/kiskaadee/Core";
          response-message = "Deployment payload dispatched successfully.";
        }
      ])} -port 9000 -verbose";
      Restart = "on-failure";
      RestartSec = "5s";
    };
  };

  # Ensure persistent directories have proper permissions for container runtimes (e.g. Stalwart UID 2000, SnappyMail)
  systemd.tmpfiles.rules = [
    "d /home/kiskaadee/Core/config/stalwart/data 0777 kiskaadee users -"
    "d /home/kiskaadee/Core/config/stalwart/etc 0777 kiskaadee users -"
    "d /home/kiskaadee/Core/config/snappymail/data 0777 kiskaadee users -"
  ];

  # Open ports in the firewall for Traefik, Gitea SSH, GitOps Webhook Receiver, and Mail (SMTP, Submission, SMTPS, IMAPS, ManageSieve)
  networking.firewall.allowedTCPPorts = [ 80 443 2223 9000 25 465 587 993 4190 ];
}
