# 🌐 Server NixOS Configuration — Turnkey Headless Homelab Appliance
# Standalone declarative configuration for the roadtotech.me homelab server.
# No Home Manager. No shared desktop modules.
# Goal: wipe & restore from any NixOS installation via `nixos-install --flake ~/Core#server`

{ config, lib, pkgs, inputs, ... }:

{
  imports = [
    ./hardware-configuration.nix      # Server-specific disk and CPU configuration
    ./modules/homeserver.nix          # Core services: systemd units, Authelia, SOPS secrets
    ./modules/traefik-deployments.nix # Traefik deployment secrets and env templates
    ./modules/dynu.nix                # Dynu DDNS smart IP monitor service
    ./modules/shell.nix               # Declarative bash environment for the server
  ];

  # ── SOPS secrets configuration ──────────────────────────────────────────────
  # Set once here to avoid duplicate-option conflicts across submodules
  sops.defaultSopsFile = ./secrets.yaml;
  sops.defaultSopsFormat = "yaml";

  # ── Nix settings ─────────────────────────────────────────────────────────────
  nix.settings.experimental-features = [ "nix-command" "flakes" ];

  # Pin nixpkgs registry to the flake input (avoids GitHub API calls from the CLI)
  nix.registry.nixpkgs.flake = inputs.nixpkgs;

  nixpkgs.config.allowUnfree = true;

  # ── Regional & locale settings ───────────────────────────────────────────────
  time.timeZone = "America/Bogota";
  i18n.defaultLocale = "en_US.UTF-8";

  # ── Console ──────────────────────────────────────────────────────────────────
  console = {
    font = "Lat2-Terminus16";
    keyMap = "us";
  };

  # ── Bootloader ───────────────────────────────────────────────────────────────
  boot.loader.systemd-boot.enable = true;
  boot.loader.efi.canTouchEfiVariables = true;

  # ── Network ──────────────────────────────────────────────────────────────────
  networking.hostName = "server";
  networking.networkmanager.enable = true;

  # ── User account ─────────────────────────────────────────────────────────────
  users.users.kiskaadee = {
    isNormalUser = true;
    openssh.authorizedKeys.keys = [
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICAVPPMk+WApUd/fo78+lsRGnLKSlY4GDZiNGKwBAifD"
    ];
    extraGroups = [
      "wheel"          # sudo access
      "docker"         # run docker without sudo
      "networkmanager" # modify network settings
    ];
  };

  # ── SSH ──────────────────────────────────────────────────────────────────────
  services.openssh = {
    enable = true;
    ports = [ 22 ];
    settings.PermitRootLogin = "no";
  };

  # ── Virtualisation ───────────────────────────────────────────────────────────
  virtualisation.docker = {
    enable = true;
    daemon.settings = {
      dns = [ "1.1.1.1" "1.0.0.1" ];
    };
  };

  # ── Dynamic binary support ───────────────────────────────────────────────────
  programs.nix-ld = {
    enable = true;
    libraries = with pkgs; [
      stdenv.cc.cc.lib
      zlib
      glib
    ];
  };

  # ── Headless optimizations ───────────────────────────────────────────────────
  # Disable peripheral hardware & desktop services not needed on a headless server
  services.pipewire.enable = false;
  services.printing.enable = false;
  hardware.bluetooth.enable = false;

  # Disable power-saving sleep/suspend to keep the homelab always active
  systemd.targets.sleep.enable = false;
  systemd.targets.suspend.enable = false;
  systemd.targets.hibernate.enable = false;
  systemd.targets.hybrid-sleep.enable = false;

  # Kernel-level power management tuning
  powerManagement.powertop.enable = true;

  # ── CLI tools ────────────────────────────────────────────────────────────────
  # Minimal server-appropriate tooling (no desktop apps or user dotfiles)
  programs.tmux.enable = true;
  programs.neovim = {
    enable = true;
    defaultEditor = true;
  };

  environment.systemPackages = with pkgs; [
    # Core orchestrator wrapper (available system-wide including non-interactive SSH)
    (writeShellScriptBin "appctl" ''
      exec "''${CORE_DIR:-$HOME/Core}/scripts/appctl" "$@"
    '')
    # Monitoring & diagnostics
    htop
    iotop
    iftop
    ncdu
    # Secrets tooling (required for sops-nix secret decryption and manual re-encryption)
    sops
    age
    # Python runtime (hard requirement for appctl and gitops_dispatcher.py)
    python3
    # Modern shell utilities
    eza        # ls replacement
    ripgrep    # rg — fast grep
    fd         # fast find
    tree       # directory tree viewer
    # Network & transfer
    git
    curl
    wget
    rclone
    jq
  ];

  # ── State version ─────────────────────────────────────────────────────────────
  # Do not modify this value after the first install.
  system.stateVersion = "26.05";
}
