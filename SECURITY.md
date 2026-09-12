# Security Policy & Hardening Overview

This document outlines the security posture of the **Hardened Private Cloud Hub** and provides guidance on maintaining a secure environment.

## 🛡️ Implemented Hardening Measures

### 1. Docker Socket Isolation (Socket-Proxy)
The most critical security feature. No container (including Traefik or Portainer) has direct access to the host's `/var/run/docker.sock`.
- **Mechanism**: The `socket-proxy` container is the *only* one with access to the real socket.
- **Filtering**: It uses HAProxy to filter API calls. Traefik can see containers and networks but cannot create/delete them. Management tools like Watchtower are granted specific POST/DELETE permissions only through this proxy.
- **Benefit**: Even if an attacker compromises Traefik, they cannot use the Docker API to spin up a privileged container and take over the host (a common "container breakout" attack).

### 2. Identity & Access Management (Authelia)
- **SSO**: All management tools (Traefik Dash, Dozzle, Portainer) and critical apps (Gitea) are behind Authelia.
- **2FA Support**: Authelia supports TOTP (Google Authenticator, etc.) and FIDO2 (Yubikey).
- **Forward Auth**: Traefik intercepts all requests and validates the session with Authelia before the request ever reaches the target app.

### 3. Network Segmentation
- **`proxy-net`**: Shared network for Traefik to route traffic to apps.
- **`socket-net`**: Highly restricted network for infrastructure-to-socket communication. Application containers are **not** members of this network.

### 4. Automated Maintenance
- **Watchtower**: Automatically updates containers when new security patches are released for their images.

---

## ⚠️ Potential Attack Vectors & Concerns

Despite the hardening, you should remain vigilant about the following:

### 1. Host-Level Security (The Foundation)
If an attacker gains SSH access to your host, they have control over the system.
- **Action**: Use SSH Keys exclusively (`PermitRootLogin = "no"` is enforced by NixOS). Keep your Nix flake inputs and system updated (`sudo nixos-rebuild switch --flake ~/Core#server`).

### 2. Application-Specific Vulnerabilities
A zero-day exploit in Gitea or Ollama could allow an attacker to execute code *inside* that specific container.
- **Mitigation**: Containers run as non-root users where possible. The `socket-proxy` ensures they cannot escape to the host easily.

### 3. Let's Encrypt / ACME Exposure
The `acme.json` file contains your private keys.
- **Action**: This file is restricted to `chmod 600`. Never share it or commit it to version control.

### 4. Denial of Service (DDoS)
Home connections are vulnerable to bandwidth saturation.
- **Mitigation**: Consider using a Cloudflare Tunnel or a VPS-based "Entry Node" if you expect high traffic or targeted attacks.

---

## 🛠️ Security Checklist for Production
- [ ] Replace all placeholder secrets in `infra/authelia/.env`.
- [ ] Hash your user passwords in `infra/authelia/config/users.yml` using `docker run --rm authelia/authelia authelia crypto hash generate pbkdf2 --password "your_password_here"`.
- [ ] Disable Traefik/Authelia access from the public internet if only local use is needed (via Firewall/IP Whitelisting).
- [ ] Ensure `acme.json` is backed up securely but kept private.
