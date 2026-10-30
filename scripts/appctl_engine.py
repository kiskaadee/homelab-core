#!/usr/bin/env python3
"""
appctl_engine.py - Authoritative Homelab Metadata Engine & Two-Axis Inspection
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import socket
import ssl
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml
from core_manifest import (
    HomepageConfig,
    Manifest,
    ManifestValidationError,
    SourceConfig,
    parse_manifest,
)
from provenance import (
    get_local_checkout_status,
    inspect_workload_two_axis,
)

# Dynamic Environment Paths
CORE_DIR = os.environ.get(
    "CORE_DIR",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)

SITES_DIR = os.environ.get(
    "SITES_DIR",
    os.path.join(os.path.dirname(CORE_DIR), "Sites")
    if os.path.isdir(os.path.join(os.path.dirname(CORE_DIR), "Sites"))
    else os.path.expanduser("~/Sites"),
)

HOMELAB_DOMAIN = (
    os.environ.get("HOMELAB_DOMAIN")
    or os.environ.get("DOMAIN_SUFFIX")
    or os.environ.get("DOMAIN", "")
)


def validate_environment() -> None:
    """Ensure mandatory environment configuration is valid before CLI execution."""
    errors: list[str] = []

    if not HOMELAB_DOMAIN:
        errors.append(
            "• Missing 'HOMELAB_DOMAIN' (or 'DOMAIN_SUFFIX' / 'DOMAIN') environment variable.\n"
            "  Please export it (e.g. export HOMELAB_DOMAIN='example.com') or set it in your environment file."
        )

    if not os.path.isdir(CORE_DIR):
        errors.append(f"• Core directory not found at: {CORE_DIR}")

    if errors:
        print("❌ Environment Validation Failed:\n", file=sys.stderr)
        for err in errors:
            print(f"  {err}", file=sys.stderr)
        sys.exit(1)


ManifestError = ManifestValidationError


@dataclass
class App:
    name: str  # Canonical identifier (e.g., "traefik", "jellyfin")
    description: str = ""  # Human-readable summary for dashboards and CLI info
    domain: str = ""  # Routing FQDN (e.g., "auth.example.com") or "internal".
    dir_path: str = (
        ""  # Filesystem directory (CORE_DIR for Core, or ~/Sites/<app> for Sites).
    )

    @property
    def is_internal(self) -> bool:
        """Returns True if the service does not expose a public TLS endpoint."""
        return (
            not self.domain
            or self.domain == "internal"
            or self.domain.endswith(".local")
            or ":" in self.domain
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON consumption by scripts/appctl"""
        data = asdict(self)
        data["type"] = getattr(self, "app_type", "app")
        return data


@dataclass
class CoreService(App):
    container: str = ""  # Target container name inside Core's docker-compose.yml
    weight: int = 50  # Homepage ordering weight
    icon: str = "default.png"  # Dashboard icon filename
    app_type: str = "core_service"
    dir_path: str = CORE_DIR

    @property
    def container_name(self) -> str:
        return self.container


@dataclass
class SitesApp(App):
    dir_name: str = ""  # directory name in ~/Sites (e.g., "homelab-vaultwarden")
    source: SourceConfig | None = (
        None  # Upstream source provenance (None = Class B Image-Only)
    )
    aliases: list[str] = field(default_factory=list)  # CLI resolution aliases
    visible: bool = True  # Defines whether the icon is visible in Homepage
    auth: bool = True  # Authelia protection guard indicator
    has_compose: bool = False  # Whether a docker-compose.yml exists
    networks: list[str] = field(
        default_factory=lambda: ["proxy-net"]
    )  # Docker network attachments
    env: dict[str, Any] = field(default_factory=dict)  # Environment variable overrides
    homepage: HomepageConfig | None = None  # Dashboard Card Configuration
    manifest_error: str | None = None  # Captured syntax/loading errors
    manifest_obj: Manifest | None = None  # Parsed authoritative Manifest
    app_type: str = "app"

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON consumption by scripts/appctl"""
        data = asdict(self)
        data.pop("manifest_obj", None)
        if self.source is not None:
            data["source"] = (
                self.source.to_dict()
                if hasattr(self.source, "to_dict")
                else self.source.model_dump()
            )
        if self.homepage is not None:
            data["homepage"] = (
                self.homepage.to_dict()
                if hasattr(self.homepage, "to_dict")
                else self.homepage.model_dump()
            )
        data["type"] = getattr(self, "app_type", "app")
        return data


def sites_app_from_manifest(
    app_dir: str,
    manifest: Manifest | dict[str, Any] | None = None,
    manifest_error: str | None = None,
) -> SitesApp:
    """Construct and normalize a SitesApp domain model from an authoritative Manifest or raw data."""
    raw_dir_name = os.path.basename(app_dir)

    if isinstance(manifest, Manifest):
        has_compose = os.path.isfile(
            os.path.join(app_dir, "docker-compose.yaml")
        ) or os.path.isfile(os.path.join(app_dir, "docker-compose.yml"))
        canonical_name = manifest.name
        domain = manifest.domain or f"{canonical_name}.{HOMELAB_DOMAIN}"
        return SitesApp(
            name=canonical_name,
            dir_name=raw_dir_name,
            source=manifest.source,
            description=manifest.description,
            domain=domain,
            dir_path=app_dir,
            has_compose=has_compose,
            aliases=list(manifest.aliases),
            visible=manifest.visible,
            auth=manifest.auth,
            networks=list(manifest.networks),
            env=dict(manifest.env),
            homepage=manifest.homepage,
            manifest_error=manifest_error,
            manifest_obj=manifest,
        )

    if isinstance(manifest, dict):
        try:
            parsed = parse_manifest(manifest)
            return sites_app_from_manifest(app_dir, parsed, manifest_error)
        except ManifestValidationError as err:
            manifest_error = f"{manifest_error}; {err}" if manifest_error else str(err)
            canonical_name = str(
                manifest.get("name") or raw_dir_name.replace("homelab-", "")
            )
            return SitesApp(
                name=canonical_name,
                dir_name=raw_dir_name,
                domain=f"{canonical_name}.{HOMELAB_DOMAIN}",
                description=str(manifest.get("description", "")),
                dir_path=app_dir,
                manifest_error=manifest_error,
            )

    canonical_name = raw_dir_name.replace("homelab-", "")
    return SitesApp(
        name=canonical_name,
        dir_name=raw_dir_name,
        description="",
        domain=f"{canonical_name}.{HOMELAB_DOMAIN}",
        dir_path=app_dir,
        manifest_error=manifest_error or "Missing app.yaml manifest",
    )


def get_sites_apps(sites_dir: str | None = None) -> list[SitesApp]:
    """Scan the given sites directory (defaulting to SITES_DIR) for applications."""
    target_dir = sites_dir or SITES_DIR
    apps: list[SitesApp] = []
    if not os.path.isdir(target_dir):
        return apps

    for entry in sorted(os.listdir(target_dir)):
        app_dir = os.path.join(target_dir, entry)
        if entry.startswith(".") or not os.path.isdir(app_dir):
            continue
        manifest_path = os.path.join(app_dir, "app.yaml")
        manifest_file = Path(manifest_path)

        if not manifest_file.is_file():
            apps.append(
                sites_app_from_manifest(
                    app_dir=app_dir,
                    manifest=None,
                    manifest_error="Missing app.yaml manifest",
                )
            )
            continue

        try:
            manifest_obj = parse_manifest(manifest_file)
            apps.append(sites_app_from_manifest(app_dir=app_dir, manifest=manifest_obj))
        except (
            ManifestValidationError,
            yaml.YAMLError,
            OSError,
            ValueError,
            KeyError,
        ) as err:
            raw_data = None
            with contextlib.suppress(Exception), open(manifest_path, "r", encoding="utf-8") as f:
                raw_data = yaml.safe_load(f)


            app_dict = raw_data if isinstance(raw_data, dict) else {}
            apps.append(
                sites_app_from_manifest(
                    app_dir=app_dir,
                    manifest=app_dict,
                    manifest_error=str(err),
                )
            )

    return apps


def get_core_services(core_dir: str | None = None) -> list[CoreService]:
    """Return the strongly-typed registry of Core platform services."""
    c_dir = core_dir or CORE_DIR
    return [
        CoreService(
            name="traefik",
            domain=f"traefik.{HOMELAB_DOMAIN}",
            container="traefik",
            description="Edge Reverse Proxy & ACME TLS",
            icon="traefik.png",
            weight=10,
            dir_path=c_dir,
        ),
        CoreService(
            name="authelia",
            domain=f"auth.{HOMELAB_DOMAIN}",
            container="authelia",
            description="Identity & SSO Access Control",
            icon="authelia.png",
            weight=20,
            dir_path=c_dir,
        ),
        CoreService(
            name="lldap",
            domain=f"users.{HOMELAB_DOMAIN}",
            container="lldap",
            description="Lightweight LDAP User Directory",
            icon="lldap.png",
            weight=25,
            dir_path=c_dir,
        ),
        CoreService(
            name="stalwart",
            domain=f"smtp.{HOMELAB_DOMAIN}",
            container="stalwart",
            description="All-in-one Mail Server",
            icon="email.png",
            weight=26,
            dir_path=c_dir,
        ),
        CoreService(
            name="dozzle",
            domain=f"logs.{HOMELAB_DOMAIN}",
            container="dozzle",
            description="Real-time Log Viewer",
            icon="dozzle.png",
            weight=40,
            dir_path=c_dir,
        ),
        CoreService(
            name="socket-proxy",
            domain="internal",
            container="socket-proxy",
            description="Docker Socket Security Proxy",
            dir_path=c_dir,
        ),
        CoreService(
            name="homepage",
            domain=f"dashboard.{HOMELAB_DOMAIN}",
            container="homepage",
            description="Application Dashboard & System Portal",
            icon="homepage.png",
            weight=5,
            dir_path=c_dir,
        ),
        CoreService(
            name="diun",
            domain="internal",
            container="diun",
            description="Docker Image Update Notifier",
            dir_path=c_dir,
        ),
        CoreService(
            name="watchtower",
            domain="internal",
            container="watchtower",
            description="Automated Container Updates",
            dir_path=c_dir,
        ),
    ]


def resolve_app(
    query: str,
    apps: list[SitesApp] | None = None,
    sites_dir: str | None = None,
    core_dir: str | None = None,
) -> App | None:
    """
    Resolve a query string to a SitesApp or CoreService.
    Priority: Canonical Name > Alias > Dir Name > Stripped 'homelab-' Prefix > Core Services.
    """
    if apps is None:
        apps = get_sites_apps(
            sites_dir=sites_dir if sites_dir is not None else SITES_DIR
        )

    c_dir = core_dir if core_dir is not None else CORE_DIR
    query_clean = query.strip().lower()

    # 1. Exact match on canonical name
    for app in apps:
        if app.name.lower() == query_clean:
            return app

    # 2. Match on alias
    for app in apps:
        if any(alias.lower() == query_clean for alias in app.aliases):
            return app

    # 3. Match directory name
    for app in apps:
        if app.dir_name.lower() == query_clean:
            return app

    # 4. Match stripped 'homelab-' prefix
    for app in apps:
        if app.dir_name.lower() == f"homelab-{query_clean}":
            return app

    # 5. Match Core services
    for svc in get_core_services(core_dir=c_dir):
        if svc.name.lower() == query_clean:
            return svc

    if query_clean == "core":
        return CoreService(
            name="core",
            domain=HOMELAB_DOMAIN,
            container="",
            description="Homelab Core Platform Stack",
            dir_path=c_dir,
            app_type="core",
        )

    return None


def get_docker_status(
    dir_path: str | Path,
    container_name: str | None = None,
) -> str:
    """Get container running status for a docker-compose directory or single Core container."""
    try:
        if container_name:
            inspect_res = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", container_name],
                capture_output=True,
                text=True,
                check=False,
            )
            if inspect_res.returncode != 0:
                err = (inspect_res.stderr or "").lower()
                if "no such object" in err or "no such container" in err:
                    return "🔴 Stopped"
                return "❓ Unknown"
            state = inspect_res.stdout.strip()
            if state.lower() == "true":
                return "🟢 Running (1)"
            return "🔴 Stopped"

        path = Path(dir_path)
        if (
            path.exists()
            and not (path / "docker-compose.yml").is_file()
            and not (path / "docker-compose.yaml").is_file()
        ):
            return "⚪ Not Stack"

        res = subprocess.run(
            ["docker", "compose", "ps", "-q"],
            cwd=str(path),
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0:
            return "❓ Unknown"

        container_ids = [c for c in res.stdout.strip().splitlines() if c]
        if not container_ids:
            return "🔴 Stopped"

        inspect_res = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}"] + container_ids,
            capture_output=True,
            text=True,
            check=False,
        )
        if inspect_res.returncode != 0:
            return "❓ Unknown"

        running_states = inspect_res.stdout.strip().splitlines()
        running_count = sum(1 for s in running_states if s.lower() == "true")
        total_count = len(container_ids)

        if running_count == total_count and total_count > 0:
            return f"🟢 Running ({running_count})"
        elif running_count > 0:
            return f"🟡 Degraded ({running_count}/{total_count})"
        else:
            return "🔴 Stopped"
    except (subprocess.SubprocessError, OSError):
        return "❓ Unknown"


def get_docker_image_revision(
    dir_path: str | Path | None = None,
    container_name: str | None = None,
) -> str | None:
    """Extract `org.opencontainers.image.revision` label from running container."""
    try:
        target: str | None = container_name
        if not target and dir_path:
            res = subprocess.run(
                ["docker", "compose", "ps", "-q"],
                cwd=str(dir_path),
                capture_output=True,
                text=True,
                check=False,
            )
            ids = [c for c in res.stdout.strip().splitlines() if c]
            if ids:
                target = ids[0]

        if not target:
            return None

        inspect_res = subprocess.run(
            [
                "docker",
                "inspect",
                "-f",
                '{{ index .Config.Labels "org.opencontainers.image.revision" }}',
                target,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        revision = inspect_res.stdout.strip()
        if revision and revision != "<no value>":
            return revision
        return None
    except (subprocess.SubprocessError, OSError):
        return None


def check_ssl_cert(
    domain: str, port: int = 443, timeout: float = 3.0
) -> dict[str, Any]:
    """Inspect SSL/TLS certificate for a domain using standard library ssl and socket."""
    if not domain or domain == "internal" or domain.endswith(".local") or ":" in domain:
        return {
            "domain": domain,
            "status": "⚪ Internal",
            "issuer": "-",
            "subject": "-",
            "days_remaining": None,
            "not_after": "-",
            "formatted_expiry": "-",
            "error": None,
        }

    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((domain, port), timeout=timeout) as sock:  # noqa: SIM117
            with ctx.wrap_socket(sock, server_hostname=domain) as ssock:
                raw_cert = ssock.getpeercert()
                cert = raw_cert if isinstance(raw_cert, dict) else {}

        not_after_str = str(cert.get("notAfter", ""))
        days_remaining = None
        formatted_expiry = "-"
        if not_after_str:
            expiry_dt = datetime.datetime.strptime(
                not_after_str, "%b %d %H:%M:%S %Y %Z"
            ).replace(tzinfo=datetime.UTC)
            now_dt = datetime.datetime.now(datetime.UTC)
            days_remaining = (expiry_dt - now_dt).days
            formatted_expiry = expiry_dt.strftime("%Y-%m-%d")

        issuer_dict: dict[str, str] = {}
        issuer_raw = cert.get("issuer")
        if isinstance(issuer_raw, (list, tuple)):
            for rdn in issuer_raw:
                if isinstance(rdn, (list, tuple)) and rdn:
                    entry = rdn[0]
                    if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                        issuer_dict[str(entry[0])] = str(entry[1])
        issuer = str(
            issuer_dict.get("organizationName")
            or issuer_dict.get("commonName")
            or "Unknown"
        )

        subject_dict: dict[str, str] = {}
        subject_raw = cert.get("subject")
        if isinstance(subject_raw, (list, tuple)):
            for rdn in subject_raw:
                if isinstance(rdn, (list, tuple)) and rdn:
                    entry = rdn[0]
                    if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                        subject_dict[str(entry[0])] = str(entry[1])
        subject = str(subject_dict.get("commonName") or domain)

        if days_remaining is not None:
            if days_remaining < 0:
                status = f"🔴 Expired ({abs(days_remaining)}d ago)"
            elif days_remaining <= 14:
                status = f"🟡 {days_remaining}d remaining"
            else:
                status = f"🟢 {days_remaining}d remaining"
        else:
            status = "🟢 Valid"

        return {
            "domain": domain,
            "status": status,
            "issuer": issuer,
            "subject": subject,
            "days_remaining": days_remaining,
            "not_after": not_after_str or "-",
            "formatted_expiry": formatted_expiry,
            "error": None,
        }
    except (
        ssl.SSLError,
        socket.gaierror,
        TimeoutError,
        ConnectionRefusedError,
        OSError,
        ValueError,
    ) as e:
        return {
            "domain": domain,
            "status": "🔴 Failed",
            "issuer": "-",
            "subject": "-",
            "days_remaining": None,
            "not_after": "-",
            "formatted_expiry": "-",
            "error": str(e),
        }


def check_all_ssl_certs(domains: list[str]) -> dict[str, dict[str, Any]]:
    """Check multiple domains concurrently with ThreadPoolExecutor."""
    valid_domains = [
        d
        for d in set(domains)
        if d and d != "internal" and not d.endswith(".local") and ":" not in d
    ]
    results: dict[str, dict[str, Any]] = {}
    if not valid_domains:
        return results

    with ThreadPoolExecutor(max_workers=min(len(valid_domains), 8)) as executor:
        future_to_domain = {
            executor.submit(check_ssl_cert, d): d for d in valid_domains
        }
        for future, d in future_to_domain.items():
            with contextlib.suppress(subprocess.SubprocessError, OSError):
                results[d] = future.result()
    return results


def cmd_sync_homepage(args: list[str] | None = None) -> None:
    """Synchronize Homepage dashboard services.yaml from app.yaml metadata."""
    args = args or []
    hp_dir = os.path.join(CORE_DIR, "config", "homepage")
    for arg in args:
        if arg.startswith("--homepage-dir="):
            hp_dir = arg.split("=", 1)[1]

    services_yaml_path = os.path.join(hp_dir, "services.yaml")
    os.makedirs(hp_dir, exist_ok=True)

    apps = get_sites_apps()
    visible_apps = [a for a in apps if a.visible and not a.manifest_error]

    groups: dict[str, list[dict[str, Any]]] = {}

    # 1. Add Core Infrastructure services
    core_services = get_core_services()
    if core_services:
        core_group = "Core Infrastructure"
        groups[core_group] = []
        for svc in core_services:
            if not svc.is_internal:
                groups[core_group].append(
                    {
                        "title": svc.name.title(),
                        "icon": getattr(svc, "icon", "default.png"),
                        "href": f"https://{svc.domain}",
                        "description": svc.description,
                        "server": "my-docker",
                        "container": getattr(svc, "container", svc.name),
                        "weight": getattr(svc, "weight", 50),
                    }
                )

    # 2. Add Sites applications
    icons_dir = os.path.join(hp_dir, "icons")
    for app in visible_apps:
        hp = app.homepage
        group_name = hp.group if hp else "Applications"
        if group_name not in groups:
            groups[group_name] = []

        card_title = hp.title if hp else app.name.title()
        icon = hp.icon if hp else f"{app.name}.png"
        container = hp.container if hp else app.name
        weight = hp.weight if hp else 50
        desc = app.description

        # Icon resolution
        base_icon, ext = os.path.splitext(icon)
        matched_icon = None
        if os.path.isdir(icons_dir) and not icon.startswith(("/", "http")):
            if os.path.isfile(os.path.join(icons_dir, icon)):
                matched_icon = icon
            elif ext and os.path.isfile(os.path.join(icons_dir, f"{base_icon}.svg")):
                matched_icon = f"{base_icon}.svg"
            elif ext and os.path.isfile(os.path.join(icons_dir, f"{base_icon}.png")):
                matched_icon = f"{base_icon}.png"
            elif not ext:
                if os.path.isfile(os.path.join(icons_dir, f"{icon}.svg")):
                    matched_icon = f"{icon}.svg"
                elif os.path.isfile(os.path.join(icons_dir, f"{icon}.png")):
                    matched_icon = f"{icon}.png"
        if matched_icon:
            icon = f"/icons/{matched_icon}"

        groups[group_name].append(
            {
                "title": card_title,
                "icon": icon,
                "href": f"https://{app.domain}",
                "description": desc,
                "server": "my-docker",
                "container": container,
                "weight": weight,
            }
        )

    # Preferred group order
    preferred_order = [
        "Core Infrastructure",
        "Knowledge & Notes",
        "Media & Productivity",
        "Development & AI",
        "Applications",
    ]
    sorted_groups: list[str] = []
    for g in preferred_order:
        if g in groups:
            sorted_groups.append(g)
    for g in sorted(groups.keys()):
        if g not in sorted_groups:
            sorted_groups.append(g)

    # Render YAML
    lines = ["---", "# 🚀 Auto-generated by appctl sync - Do not edit directly"]
    for group_name in sorted_groups:
        cards = sorted(groups[group_name], key=lambda x: (x["weight"], x["title"]))
        lines.append(f"- {group_name}:")
        for card in cards:
            lines.append(f"    - {card['title']}:")
            lines.append(f"        icon: {card['icon']}")
            lines.append(f"        href: {card['href']}")
            if card["description"]:
                lines.append(f"        description: {card['description']}")
            lines.append(f"        server: {card['server']}")
            lines.append(f"        container: {card['container']}")
        lines.append("")

    content = "\n".join(lines).strip() + "\n"
    with open(services_yaml_path, "w", encoding="utf-8") as f:
        f.write(content)

    print(
        f"✨ Homepage synchronized successfully ({len(visible_apps)} visible apps in {len(sorted_groups)} groups)"
    )
    print(f"📄 Generated {services_yaml_path}")


def cmd_list(args: list[str]) -> None:
    """Format and print application and core stack listings with two-axis status and optional SSL cert check."""
    show_core = any(a in ["--core", "-c", "--all", "-a"] for a in args)
    should_fetch = any(a in ["--fetch", "-f"] for a in args)
    show_ssl = any(a in ["--ssl", "-s"] for a in args)
    apps = get_sites_apps()

    ssl_data: dict[str, dict[str, Any]] = {}
    if show_ssl:
        domains_to_check = [app.domain for app in apps]
        if show_core:
            core_services = get_core_services()
            domains_to_check.extend([svc.domain for svc in core_services])
        print("🔒 Inspecting SSL/TLS certificates...")
        ssl_data = check_all_ssl_certs(domains_to_check)

    if show_ssl:
        print(
            f"{'SERVICE':<16} {'DEPLOYMENT':<22} {'PROVENANCE':<22} {'STATUS':<15} {'DOMAIN':<28} {'CERT'}"
        )
        print(
            f"{'-------':<16} {'----------':<22} {'----------':<22} {'------':<15} {'------':<28} {'----'}"
        )
    else:
        print(
            f"{'SERVICE':<16} {'DEPLOYMENT':<22} {'PROVENANCE':<24} {'STATUS':<15} {'DOMAIN':<28}"
        )
        print(
            f"{'-------':<16} {'----------':<22} {'----------':<24} {'------':<15} {'------':<28}"
        )

    for app in apps:
        app_path = Path(app.dir_path)
        docker_status = get_docker_status(app_path)

        if app.manifest_error:
            dep_str = "⚠️ Manifest Error"
            prov_str = "⚠️ Invalid Manifest"
        else:
            manifest_obj = app.manifest_obj or Manifest(
                name=app.name, domain=app.domain
            )
            two_axis = inspect_workload_two_axis(
                app_dir=app_path,
                manifest=manifest_obj,
                fetch_upstream=should_fetch,
                docker_status=docker_status,
            )
            dep_str = two_axis.checkout.status_label
            prov_str = two_axis.provenance.status.value

        if show_ssl:
            cert_info = ssl_data.get(app.domain, check_ssl_cert(app.domain))
            cert_badge = cert_info.get("status", "-")
            print(
                f"{app.name:<16} {dep_str:<22} {prov_str:<22} {docker_status:<15} {app.domain:<28} {cert_badge}"
            )
        else:
            print(
                f"{app.name:<16} {dep_str:<22} {prov_str:<24} {docker_status:<15} {app.domain:<28}"
            )

    if show_core:
        core_path = Path(CORE_DIR)
        core_checkout = get_local_checkout_status(core_path)
        core_dep_str = core_checkout.status_label
        print()
        if show_ssl:
            print(
                f"{'CORE SERVICE':<16} {'DEPLOYMENT':<22} {'PROVENANCE':<22} {'STATUS':<15} {'DOMAIN':<28} {'CERT'}"
            )
            print(
                f"{'------------':<16} {'----------':<22} {'----------':<22} {'------':<15} {'------':<28} {'----'}"
            )
        else:
            print(
                f"{'CORE SERVICE':<16} {'DEPLOYMENT':<22} {'PROVENANCE':<24} {'STATUS':<15} {'DOMAIN':<28}"
            )
            print(
                f"{'------------':<16} {'----------':<22} {'----------':<24} {'------':<15} {'------':<28}"
            )

        core_services = get_core_services()
        for i, svc in enumerate(core_services):
            row_dep = core_dep_str if i == 0 else ""
            svc_docker = get_docker_status(core_path, container_name=svc.container)
            if show_ssl:
                cert_info = ssl_data.get(svc.domain, check_ssl_cert(svc.domain))
                cert_badge = cert_info.get("status", "-")
                print(
                    f"{svc.name:<16} {row_dep:<22} {'— Core Platform':<22} {svc_docker:<15} {svc.domain:<28} {cert_badge}"
                )
            else:
                print(
                    f"{svc.name:<16} {row_dep:<22} {'— Core Platform':<24} {svc_docker:<15} {svc.domain:<28}"
                )


def cmd_info(args: list[str]) -> None:
    """Show detailed two-axis diagnostics and metadata for a workload or Core service."""
    clean_args = [a for a in args if not a.startswith("-")]
    if not clean_args:
        print("❌ Error: Service name required for 'info' command")
        sys.exit(1)

    show_ssl = any(a in ["--ssl", "-s"] for a in args)
    query = clean_args[0]
    app = resolve_app(query)

    if not app:
        print(f"❌ Error: Service '{query}' not found under {SITES_DIR} or Core.")
        sys.exit(1)

    if isinstance(app, CoreService):
        print(f"ℹ️  Core Infrastructure Service: {app.name}")
        print("-" * 50)
        print(f"Directory:       {app.dir_path}")
        print(f"Domain:          {app.domain}")
        print(f"Container:       {app.container}")
        print(
            f"Status:          {get_docker_status(Path(app.dir_path), app.container)}"
        )
        print(f"Description:     {app.description}")
        return

    if not isinstance(app, SitesApp):
        return

    # SitesApp
    print(f"ℹ️  Workload Overview: {app.name}")

    print("-" * 50)
    print(f"Directory:       {app.dir_path}")
    print(f"Domain:          {app.domain}")
    print(f"Description:     {app.description}")
    if app.aliases:
        print(f"Aliases:         {', '.join(app.aliases)}")
    print(
        f"Class:           {'Class A (Source-Backed)' if app.source else 'Class B (Image-Only)'}"
    )
    print(f"Networks:        {', '.join(app.networks)}")
    print(f"Auth Protected:  {app.auth}")
    print(f"Dashboard:       {'Visible' if app.visible else 'Hidden'}")

    if app.manifest_error:
        print(f"\n⚠️  Manifest Validation Error:\n  {app.manifest_error}")
        return

    app_path = Path(app.dir_path)
    manifest_obj = app.manifest_obj or Manifest(name=app.name, domain=app.domain)

    two_axis = inspect_workload_two_axis(
        app_dir=app_path,
        manifest=manifest_obj,
        fetch_upstream=True,
        docker_status=get_docker_status(app_path),
        image_revision=get_docker_image_revision(app_path),
    )

    print("\n📦 Deployment Axis (Local State):")
    print(f"  Git Repo:      {'Yes' if two_axis.checkout.is_git else 'No'}")
    if two_axis.checkout.is_git:
        print(f"  Branch:        {two_axis.checkout.branch}")
        print(f"  Commit SHA:    {two_axis.checkout.commit_sha}")
        print(
            f"  Checkout:      {'* Dirty (Uncommitted changes)' if two_axis.checkout.is_dirty else '✓ Clean'}"
        )
    print(f"  Docker Status: {two_axis.docker_status}")
    if two_axis.image_revision:
        print(f"  Image Revision:{two_axis.image_revision}")

    print("\n🌐 Provenance Axis (Upstream):")
    if app.source:
        print(f"  Source URL:    {app.source.url}")
        print(f"  Target Ref:    {app.source.ref}")
        print(f"  Status:        {two_axis.provenance.status.value}")
        print(f"  Details:       {two_axis.provenance.message}")
    else:
        print("  Source:        None (Image-Only workload)")
        print(f"  Status:        {two_axis.provenance.status.value}")

    if show_ssl and not app.is_internal:
        cert_info = check_ssl_cert(app.domain)
        print("\n🔒 SSL/TLS Certificate:")
        print(f"  Status:        {cert_info['status']}")
        print(f"  Issuer:        {cert_info['issuer']}")
        print(f"  Expiration:    {cert_info['formatted_expiry']}")


def cmd_resolve(args: list[str]) -> None:
    """Resolve a workload or core service and emit JSON for scripts/appctl."""
    if not args:
        sys.exit(1)
    app = resolve_app(args[0])
    if not app:
        sys.exit(1)
    if isinstance(app, SitesApp) and app.manifest_error:
        print(
            f"❌ Error: Workload '{app.name}' has invalid manifest: {app.manifest_error}",
            file=sys.stderr,
        )
        sys.exit(1)
    print(json.dumps(app.to_dict()))


def cmd_ssl(args: list[str]) -> None:
    """Check SSL/TLS certificates across services or for an individual service."""
    targets = [a for a in args if not a.startswith("-")]
    if targets:
        for target in targets:
            app = resolve_app(target)
            if not app:
                print(f"❌ Error: Service '{target}' not found in ~/Sites or Core.")
                continue
            if app.is_internal:
                print(f"ℹ️  {app.name} ({app.domain}) is internal / no public TLS.")
                continue

            print(f"🔒 SSL/TLS Certificate: {app.name} ({app.domain})")
            print("-" * 50)
            cert_info = check_ssl_cert(app.domain)
            print(f"Domain:          {cert_info['domain']}")
            print(f"Status:          {cert_info['status']}")
            print(f"Issuer:          {cert_info['issuer']}")
            print(f"Subject:         {cert_info['subject']}")
            print(f"Expiration:      {cert_info['not_after']}")
            if cert_info["days_remaining"] is not None:
                print(
                    f"Days Remaining:  {cert_info['days_remaining']} days ({cert_info['formatted_expiry']})"
                )
            if cert_info["error"]:
                print(f"Details/Error:   {cert_info['error']}")
            print()
        return

    # Check all domains
    apps = get_sites_apps()
    services_to_check = [(app.name, app.domain) for app in apps if not app.is_internal]
    for svc in get_core_services():
        if not svc.is_internal:
            services_to_check.append((f"{svc.name} (core)", svc.domain))

    domains = [d for _, d in services_to_check]
    print(f"🔒 Checking SSL/TLS certificates for {len(domains)} service(s)...")
    ssl_results = check_all_ssl_certs(domains)

    print(f"\n{'SERVICE':<20} {'DOMAIN':<30} {'STATUS':<24} {'ISSUER':<20} {'EXPIRES'}")
    print(f"{'-------':<20} {'------':<30} {'------':<24} {'------':<20} {'-------'}")

    for name, domain in services_to_check:
        info = ssl_results.get(domain, check_ssl_cert(domain))
        print(
            f"{name:<20} {domain:<30} {info['status']:<24} {info['issuer']:<20} {info['formatted_expiry']}"
        )


def cmd_complete(args: list[str]) -> None:
    """Print autocompletion candidates for bash wrapper."""
    words: set[str] = set()
    for app in get_sites_apps():
        words.add(app.name)
        for a in app.aliases:
            words.add(a)
    for svc in get_core_services():
        words.add(svc.name)
    words.add("core")
    print(" ".join(sorted(words)))


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(1)

    command = sys.argv[1]
    args = sys.argv[2:]

    if command in ("list", "status"):
        cmd_list(args)
    elif command == "ssl":
        cmd_ssl(args)
    elif command == "info":
        cmd_info(args)
    elif command == "resolve":
        cmd_resolve(args)
    elif command == "complete":
        cmd_complete(args)
    elif command == "sync":
        cmd_sync_homepage(args)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
