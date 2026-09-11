# 🌐 Dynu DDNS IP Monitor & Updater Service
# This NixOS module configures a smart, local-first IP change detector.
# It queries public IP providers, maintains a history log of WAN IP rotations,
# and triggers ddclient only when an actual change is detected to prevent API rate limits.

{ config, lib, pkgs, ... }:

let
  # Declaratively read the external Python script into the Nix store at evaluation time
  ipMonitorScript = pkgs.writers.writePython3Bin "dynu-ip-monitor" { } (builtins.readFile ../scripts/monitor.py);
in
{
  # Define the keys to decrypt
  sops.secrets."dynu/user" = { };
  sops.secrets."dynu/domain" = { };
  sops.secrets."dynu/password" = { };
  sops.secrets."system/pdf_decrypt_password" = {
    owner = "kiskaadee";
  };

  # Assemble the ddclient config file securely at boot time in /run/secrets/
  # Defaults to owner root:root (which avoids dynamic user evaluation issues)
  sops.templates."ddclient.conf" = {
    content = builtins.replaceStrings
      [ "@dynu_user@" "@dynu_password@" "@dynu_domain@" ]
      [
        config.sops.placeholder."dynu/user"
        config.sops.placeholder."dynu/password"
        config.sops.placeholder."dynu/domain"
      ]
      (builtins.readFile ../scripts/ddclient.conf);
  };

  # Enable ddclient and feed it the decrypted configuration template via systemd credentials
  services.ddclient = {
    enable = true;
    configFile = "/run/credentials/ddclient.service/ddclient.conf";
  };

  systemd.services.ddclient = {
    serviceConfig = {
      # Securely load the root-owned secret file into the dynamic ddclient service
      LoadCredential = [ "ddclient.conf:${config.sops.templates."ddclient.conf".path}" ];
    };
  };

  # Disable ddclient's automatic timer.
  # This stops ddclient from polling every 10 minutes on its own.
  systemd.timers.ddclient.enable = false;

  # Define the smart IP change detector service
  systemd.services.dynu-monitor = {
    description = "Dynu DDNS Smart IP Change Monitor";
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    path = with pkgs; [
      systemd     # Required for systemctl
    ];
    serviceConfig = {
      Type = "oneshot";
      User = "root";
      Group = "root";
      StateDirectory = "dynu";
      Environment = "HISTORY_FILE=/var/lib/dynu/ip_history.jsonl";
      ExecStart = "${ipMonitorScript}/bin/dynu-ip-monitor";
      TimeoutSec = 30;

      # Hardening
      NoNewPrivileges = true;
      PrivateTmp = true;
      ProtectSystem = "strict";
      ProtectHome = true;
      ReadWritePaths = "/var/lib/dynu";
    };
  };

  # Run the smart IP change monitor periodically (every 30 minutes)
  systemd.timers.dynu-monitor = {
    description = "Run Dynu IP monitor periodically";
    timerConfig = {
      OnBootSec = "5min";
      OnUnitActiveSec = "30min";
      AccuracySec = "1min";
      Persistent = true;
    };
    wantedBy = [ "timers.target" ];
  };
}
