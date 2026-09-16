#!/usr/bin/env python3
"""
appctl_engine.py - Homelab Metadata Engine, Orchestration Parser & Git Sync Monitor
"""

import contextlib
import datetime
import json
import os
import socket
import ssl
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

CORE_DIR = os.environ.get(
    "CORE_DIR",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)
SITES_DIR = os.environ.get(
    "SITES_DIR",
    os.path.join(os.path.dirname(CORE_DIR), "Sites")
    if os.path.isdir(os.path.join(os.path.dirname(CORE_DIR), "Sites"))
    else (
        os.path.expanduser("~/Sites")
        if os.path.isdir(os.path.expanduser("~/Sites"))
        else os.path.expanduser("~/Homelab/Sites")
    ),
)
ENV_FILE = os.environ.get("ENV_FILE", "/run/secrets/rendered/traefik-deployments.env")
if not os.path.isfile(ENV_FILE) and os.path.isfile("/run/secrets/traefik-deployments.env"):
    ENV_FILE = "/run/secrets/traefik-deployments.env"


def parse_yaml_simple(text):
    """Simple, zero-dependency YAML parser tailored for app.yaml schemas."""
    data = {}
    current_key = None
    lines = text.splitlines()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))

        # List element
        if stripped.startswith("- "):
            item = stripped[2:].strip().strip("\"'")
            if current_key:
                if not isinstance(data.get(current_key), list):
                    data[current_key] = []
                data[current_key].append(item)
            continue

        if ":" in stripped:
            k, v = stripped.split(":", 1)
            k = k.strip()
            v = v.strip().strip("\"'")

            if v.lower() == "true":
                v = True
            elif v.lower() == "false":
                v = False
            elif v.isdigit():
                v = int(v)

            if indent == 0:
                current_key = k
                if v == "":
                    data[k] = {}
                else:
                    data[k] = v
            elif indent > 0 and current_key:
                if not isinstance(data.get(current_key), dict):
                    data[current_key] = {}
                data[current_key][k] = v

    return data


def get_all_apps():
    """Scan SITES_DIR for all application directories and parse app.yaml."""
    apps = []
    if not os.path.isdir(SITES_DIR):
        return apps

    for entry in sorted(os.listdir(SITES_DIR)):
        app_dir = os.path.join(SITES_DIR, entry)
        if not os.path.isdir(app_dir):
            continue

        manifest_path = os.path.join(app_dir, "app.yaml")
        manifest = {}
        if os.path.isfile(manifest_path):
            with (
                contextlib.suppress(OSError, UnicodeDecodeError, ValueError, KeyError, AttributeError),
                open(manifest_path, encoding="utf-8") as f,
            ):
                manifest = parse_yaml_simple(f.read())

        canonical_name = manifest.get("name") or entry.replace("homelab-", "")
        aliases = manifest.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]

        app_info = {
            "dir_name": entry,
            "dir_path": app_dir,
            "name": canonical_name,
            "aliases": aliases,
            "domain": manifest.get("domain", f"{canonical_name}.roadtotech.me"),
            "description": manifest.get("description", ""),
            "visible": manifest.get("visible", True),
            "auth": manifest.get("auth", False),
            "networks": manifest.get("networks", ["proxy-net"]),
            "env": manifest.get("env", {}),
            "homepage": manifest.get("homepage", {}),
            "has_compose": os.path.isfile(os.path.join(app_dir, "docker-compose.yml")),
        }
        apps.append(app_info)
    return apps


def resolve_app(query):
    """Resolve an app query (canonical name, alias, or folder name)."""
    apps = get_all_apps()
    query_clean = query.strip().lower()

    # 1. Exact match on canonical name
    for app in apps:
        if app["name"].lower() == query_clean:
            return app

    # 2. Match on alias
    for app in apps:
        for alias in app["aliases"]:
            if str(alias).lower() == query_clean:
                return app

    # 3. Match on directory name
    for app in apps:
        if app["dir_name"].lower() == query_clean:
            return app

    # 4. Match stripped 'homelab-' prefix
    for app in apps:
        if app["dir_name"].lower() == f"homelab-{query_clean}":
            return app

    return None


def get_docker_status(dir_path):
    """Get container running status for a docker-compose directory."""
    if not os.path.isdir(dir_path) or not os.path.isfile(os.path.join(dir_path, "docker-compose.yml")):
        return "⚪ Not Stack"

    try:
        res = subprocess.run(
            ["docker", "compose", "ps", "-q"],
            cwd=dir_path,
            capture_output=True,
            text=True,
            check=False,
        )
        container_ids = [c for c in res.stdout.strip().splitlines() if c]
        if not container_ids:
            return "🔴 Stopped"

        inspect_res = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}"] + container_ids,
            capture_output=True,
            text=True,
            check=False,
        )
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


def get_git_sync_status(dir_path):
    """Inspect local git repository tracking status against remote."""
    if not os.path.isdir(os.path.join(dir_path, ".git")):
        return "⚪ Non-Git"

    try:
        # Check dirty uncommitted status
        status_res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=dir_path,
            capture_output=True,
            text=True,
            check=False,
        )
        is_dirty = bool(status_res.stdout.strip())

        # Check upstream branch
        upstream_res = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            cwd=dir_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if upstream_res.returncode != 0:
            return "⚪ Untracked *" if is_dirty else "⚪ Untracked"

        # Check ahead / behind counts
        rev_res = subprocess.run(
            ["git", "rev-list", "--left-right", "--count", "HEAD...@{u}"],
            cwd=dir_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if rev_res.returncode != 0:
            return "❓ Sync Error"

        parts = rev_res.stdout.strip().split()
        if len(parts) != 2:
            return "❓ Unknown"

        ahead = int(parts[0])
        behind = int(parts[1])

        if ahead == 0 and behind == 0:
            badge = "✓ Synced"
        elif ahead > 0 and behind == 0:
            badge = f"⬆ {ahead} Ahead"
        elif ahead == 0 and behind > 0:
            badge = f"⬇ {behind} Behind"
        else:
            badge = f"⚡ {ahead}⬆ {behind}⬇"

        if is_dirty:
            badge += " *"

        return badge
    except (subprocess.SubprocessError, OSError, ValueError):
        return "❓ Unknown"


def get_git_diagnostics(dir_path):
    """Get detailed git diagnostics for info command."""
    if not os.path.isdir(os.path.join(dir_path, ".git")):
        return None

    diag = {}
    with contextlib.suppress(subprocess.SubprocessError, OSError):
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=dir_path,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        diag["branch"] = branch or "HEAD (detached)"

        remote = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=dir_path,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        diag["remote"] = remote or "None"

        upstream = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            cwd=dir_path,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        diag["upstream"] = upstream or "None"

        diag["sync_badge"] = get_git_sync_status(dir_path)

        status_porcelain = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=dir_path,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        diag["dirty_files"] = status_porcelain.splitlines() if status_porcelain else []
    return diag


def fetch_repository(dir_path):
    """Run git fetch on a single repository with timeout."""
    if os.path.isdir(os.path.join(dir_path, ".git")):
        with contextlib.suppress(subprocess.SubprocessError, OSError):
            subprocess.run(
                ["git", "fetch", "--quiet"],
                cwd=dir_path,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )


def fetch_all_repositories(apps, include_core=True):
    """Fetch all repositories concurrently."""
    dirs_to_fetch = [app["dir_path"] for app in apps]
    if include_core and os.path.isdir(CORE_DIR):
        dirs_to_fetch.append(CORE_DIR)

    with ThreadPoolExecutor(max_workers=8) as executor:
        executor.map(fetch_repository, dirs_to_fetch)


def get_core_services():
    """Inspect Core stack from CORE_DIR."""
    core_services = [
        {"name": "traefik", "domain": "traefik.roadtotech.me", "container": "traefik", "desc": "Edge Reverse Proxy & ACME TLS"},
        {"name": "authelia", "domain": "auth.roadtotech.me", "container": "authelia", "desc": "Identity & SSO Access Control"},
        {"name": "lldap", "domain": "users.roadtotech.me", "container": "lldap", "desc": "Lightweight LDAP User & Group Directory"},
        {"name": "stalwart", "domain": "mail.roadtotech.me", "container": "stalwart", "desc": "All-in-one Mail Server & JMAP/IMAP/SMTP"},
        {"name": "snappymail", "domain": "webmail.roadtotech.me", "container": "snappymail", "desc": "Modern Lightweight Webmail Client"},
        {"name": "portainer", "domain": "portainer.roadtotech.me", "container": "portainer", "desc": "Container Management GUI"},
        {"name": "dozzle", "domain": "logs.roadtotech.me", "container": "dozzle", "desc": "Real-time Log Viewer"},
        {"name": "socket-proxy", "domain": "internal", "container": "socket-proxy", "desc": "Docker Socket Security Proxy"},
        {"name": "homepage", "domain": "dashboard.roadtotech.me", "container": "homepage", "desc": "Application Dashboard & System Portal"},
        {"name": "diun", "domain": "internal", "container": "diun", "desc": "Docker Image Update Notifier"},
        {"name": "watchtower", "domain": "internal", "container": "watchtower", "desc": "Automated Container Updates"},
    ]
    for svc in core_services:
        try:
            inspect_res = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", svc["container"]],
                capture_output=True,
                text=True,
                check=False,
            )
            state = inspect_res.stdout.strip()
            if state.lower() == "true":
                svc["status"] = "🟢 Running (1)"
            else:
                svc["status"] = "🔴 Stopped"
        except (subprocess.SubprocessError, OSError):
            svc["status"] = "❓ Unknown"
    return core_services


def check_ssl_cert(domain, port=443, timeout=3.0):
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
        with (
            socket.create_connection((domain, port), timeout=timeout) as sock,
            ctx.wrap_socket(sock, server_hostname=domain) as ssock,
        ):
            cert = ssock.getpeercert()

        not_after_str = cert.get("notAfter")
        days_remaining = None
        formatted_expiry = "-"
        if not_after_str:
            expiry_dt = datetime.datetime.strptime(not_after_str, "%b %d %H:%M:%S %Y %Z").replace(
                tzinfo=datetime.timezone.utc
            )
            now_dt = datetime.datetime.now(datetime.timezone.utc)
            days_remaining = (expiry_dt - now_dt).days
            formatted_expiry = expiry_dt.strftime("%Y-%m-%d")

        issuer_dict = dict(x[0] for x in cert.get("issuer", ()))
        issuer = issuer_dict.get("organizationName") or issuer_dict.get("commonName") or "Unknown"

        subject_dict = dict(x[0] for x in cert.get("subject", ()))
        subject = subject_dict.get("commonName") or domain

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
    except (ssl.SSLError, socket.gaierror, TimeoutError, ConnectionRefusedError, OSError, ValueError) as e:
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


def check_all_ssl_certs(domains):
    """Check multiple domains concurrently with ThreadPoolExecutor."""
    valid_domains = [d for d in set(domains) if d and d != "internal" and not d.endswith(".local") and ":" not in d]
    results = {}
    if not valid_domains:
        return results

    with ThreadPoolExecutor(max_workers=min(len(valid_domains), 8)) as executor:
        future_to_domain = {executor.submit(check_ssl_cert, d): d for d in valid_domains}
        for future, d in future_to_domain.items():
            with contextlib.suppress(subprocess.SubprocessError, OSError):
                results[d] = future.result()
    return results


def cmd_list(args):
    """Format and print application and core stack listings with Git sync status and optional SSL cert check."""
    show_core = any(a in ["--core", "-c", "--all", "-a"] for a in args)
    should_fetch = any(a in ["--fetch", "-f"] for a in args)
    show_ssl = any(a in ["--ssl", "-s"] for a in args)
    apps = get_all_apps()

    if should_fetch:
        print("📡 Fetching remote updates across all repositories...")
        fetch_all_repositories(apps, include_core=show_core)

    ssl_data = {}
    if show_ssl:
        domains_to_check = [app["domain"] for app in apps]
        if show_core:
            core_services = get_core_services()
            domains_to_check.extend([svc["domain"] for svc in core_services])
        print("🔒 Inspecting SSL/TLS certificates...")
        ssl_data = check_all_ssl_certs(domains_to_check)

    if show_ssl:
        print(f"{'SERVICE':<16} {'STATUS':<15} {'GIT SYNC':<15} {'DOMAIN':<28} {'CERT STATUS':<22} {'ISSUER'}")
        print(f"{'-------':<16} {'------':<15} {'--------':<15} {'------':<28} {'-----------':<22} {'------'}")
    else:
        print(f"{'SERVICE':<16} {'STATUS':<15} {'GIT SYNC':<15} {'DOMAIN':<28} {'DIRECTORY'}")
        print(f"{'-------':<16} {'------':<15} {'--------':<15} {'------':<28} {'---------'}")

    for app in apps:
        status = get_docker_status(app["dir_path"])
        git_status = get_git_sync_status(app["dir_path"])
        if show_ssl:
            cert_info = ssl_data.get(app["domain"], check_ssl_cert(app["domain"]))
            print(f"{app['name']:<16} {status:<15} {git_status:<15} {app['domain']:<28} {cert_info['status']:<22} {cert_info['issuer']}")
        else:
            rel_dir = app["dir_path"].replace(os.path.expanduser("~"), "~")
            print(f"{app['name']:<16} {status:<15} {git_status:<15} {app['domain']:<28} {rel_dir}")

    if show_core:
        core_git = get_git_sync_status(CORE_DIR)
        print()
        if show_ssl:
            print(f"{'CORE SERVICE':<16} {'STATUS':<15} {'GIT SYNC':<15} {'DOMAIN':<28} {'CERT STATUS':<22} {'ISSUER'}")
            print(f"{'------------':<16} {'------':<15} {'--------':<15} {'------':<28} {'-----------':<22} {'------'}")
        else:
            print(f"{'CORE SERVICE':<16} {'STATUS':<15} {'GIT SYNC':<15} {'DOMAIN':<28} {'DIRECTORY'}")
            print(f"{'------------':<16} {'------':<15} {'--------':<15} {'------':<28} {'---------'}")
        core_services = get_core_services()
        rel_core = CORE_DIR.replace(os.path.expanduser("~"), "~")
        for i, svc in enumerate(core_services):
            row_git = core_git if i == 0 else ""
            if show_ssl:
                cert_info = ssl_data.get(svc["domain"], check_ssl_cert(svc["domain"]))
                print(f"{svc['name']:<16} {svc['status']:<15} {row_git:<15} {svc['domain']:<28} {cert_info['status']:<22} {cert_info['issuer']}")
            else:
                print(f"{svc['name']:<16} {svc['status']:<15} {row_git:<15} {svc['domain']:<28} {rel_core}")


def cmd_ssl(args):
    """Check SSL/TLS certificates across services or for an individual service."""
    targets = [a for a in args if not a.startswith("-")]

    if targets:
        for target in targets:
            app = resolve_app(target)
            domain = None
            name = target
            if app:
                domain = app["domain"]
                name = app["name"]
            else:
                for svc in get_core_services():
                    if svc["name"].lower() == target.lower():
                        domain = svc["domain"]
                        name = svc["name"]
                        break

            if not domain:
                print(f"❌ Error: Service '{target}' not found in ~/Sites or Core.")
                continue

            print(f"🔒 SSL/TLS Certificate: {name} ({domain})")
            print("-" * 50)
            cert_info = check_ssl_cert(domain)
            print(f"Domain:          {cert_info['domain']}")
            print(f"Status:          {cert_info['status']}")
            print(f"Issuer:          {cert_info['issuer']}")
            print(f"Subject:         {cert_info['subject']}")
            print(f"Expiration:      {cert_info['not_after']}")
            if cert_info["days_remaining"] is not None:
                print(f"Days Remaining:  {cert_info['days_remaining']} days ({cert_info['formatted_expiry']})")
            if cert_info["error"]:
                print(f"Details/Error:   {cert_info['error']}")
            print()
        return

    # Check all domains across apps and core
    apps = get_all_apps()
    services_to_check = [(app["name"], app["domain"]) for app in apps]
    for svc in get_core_services():
        if svc["domain"] != "internal":
            services_to_check.append((f"{svc['name']} (core)", svc["domain"]))

    domains = [d for _, d in services_to_check]
    print(f"🔒 Checking SSL/TLS certificates for {len(domains)} service(s)...")
    ssl_results = check_all_ssl_certs(domains)

    print(f"\n{'SERVICE':<20} {'DOMAIN':<30} {'STATUS':<24} {'ISSUER':<20} {'EXPIRES'}")
    print(f"{'-------':<20} {'------':<30} {'------':<24} {'------':<20} {'-------'}")

    for name, domain in services_to_check:
        info = ssl_results.get(domain, check_ssl_cert(domain))
        print(f"{name:<20} {domain:<30} {info['status']:<24} {info['issuer']:<20} {info['formatted_expiry']}")


def cmd_info(args):
    """Show detailed metadata, runtime overview, and Git diagnostics for a service."""
    clean_args = [a for a in args if not a.startswith("-")]
    if not clean_args:
        print("❌ Error: Service name required for 'info' command")
        sys.exit(1)

    show_ssl = any(a in ["--ssl", "-s"] for a in args)
    query = clean_args[0]
    app = resolve_app(query)
    if not app:
        # Check if it's a core service
        core_services = get_core_services()
        for svc in core_services:
            if svc["name"] == query:
                print(f"ℹ️  Core Infrastructure Service: {svc['name']}")
                print("-" * 45)
                print(f"Directory:       {CORE_DIR}")
                print(f"Status:          {svc['status']}")
                print(f"Primary Domain:  https://{svc['domain']}" if svc["domain"] != "internal" else f"Domain:          {svc['domain']}")
                print(f"Description:     {svc['desc']}")

                core_diag = get_git_diagnostics(CORE_DIR)
                if core_diag:
                    print("\nGit Repository (Core):")
                    print(f"  Branch:        {core_diag['branch']}")
                    print(f"  Upstream:      {core_diag['upstream']}")
                    print(f"  Remote URL:    {core_diag['remote']}")
                    print(f"  Sync Status:   {core_diag['sync_badge']}")
                    if core_diag["dirty_files"]:
                        print(f"  Dirty Files:   {len(core_diag['dirty_files'])} uncommitted file(s)")

                if show_ssl and svc["domain"] != "internal":
                    cert_info = check_ssl_cert(svc["domain"])
                    print("\nSSL/TLS Certificate:")
                    print(f"  Status:        {cert_info['status']}")
                    print(f"  Issuer:        {cert_info['issuer']}")
                    print(f"  Subject:       {cert_info['subject']}")
                    print(f"  Expiration:    {cert_info['not_after']}")
                    if cert_info["days_remaining"] is not None:
                        print(f"  Days Left:     {cert_info['days_remaining']} day(s) ({cert_info['formatted_expiry']})")
                    if cert_info["error"]:
                        print(f"  Details/Error: {cert_info['error']}")
                return

        print(f"❌ Error: Application '{query}' not found under {SITES_DIR}")
        sys.exit(1)

    status = get_docker_status(app["dir_path"])
    aliases_str = ", ".join(app["aliases"]) if app["aliases"] else "None"
    auth_str = "🔒 Protected (Authelia)" if app["auth"] else "⚪ Disabled (Public)"
    visible_str = f"🟢 Visible ({app['homepage'].get('group', 'Default')})" if app["visible"] else "⚪ Hidden"
    networks_str = ", ".join(app["networks"]) if app["networks"] else "proxy-net"

    print(f"ℹ️  Application Information: {app['name']}")
    print("-" * 45)
    print(f"Directory:       {app['dir_path']}")
    print(f"Canonical Name:  {app['name']}")
    print(f"Aliases:         {aliases_str}")
    print(f"Status:          {status}")
    print(f"Primary Domain:  https://{app['domain']}")
    print(f"Authelia Guard:  {auth_str}")
    print(f"Dashboard View:  {visible_str}")
    print(f"Networks:        {networks_str}")
    if app["description"]:
        print(f"Description:     {app['description']}")

    # Git Diagnostics
    git_diag = get_git_diagnostics(app["dir_path"])
    if git_diag:
        print("\nGit Repository Diagnostics:")
        print(f"  Branch:        {git_diag['branch']}")
        print(f"  Upstream:      {git_diag['upstream']}")
        print(f"  Remote URL:    {git_diag['remote']}")
        print(f"  Sync Status:   {git_diag['sync_badge']}")
        if git_diag["dirty_files"]:
            print(f"  Dirty Files:   {len(git_diag['dirty_files'])} uncommitted file(s)")
            for f in git_diag["dirty_files"][:5]:
                print(f"    - {f}")
            if len(git_diag["dirty_files"]) > 5:
                print(f"    ... and {len(git_diag['dirty_files']) - 5} more")

    if show_ssl and app["domain"] != "internal":
        cert_info = check_ssl_cert(app["domain"])
        print("\nSSL/TLS Certificate:")
        print(f"  Status:        {cert_info['status']}")
        print(f"  Issuer:        {cert_info['issuer']}")
        print(f"  Subject:       {cert_info['subject']}")
        print(f"  Expiration:    {cert_info['not_after']}")
        if cert_info["days_remaining"] is not None:
            print(f"  Days Left:     {cert_info['days_remaining']} day(s) ({cert_info['formatted_expiry']})")
        if cert_info["error"]:
            print(f"  Details/Error: {cert_info['error']}")

    if app["env"]:
        print("\nConfigured Environment Defaults:")
        for k, v in app["env"].items():
            print(f"  {k} = {v}")

    if app["visible"] and app["homepage"]:
        hp = app["homepage"]
        print("\nHomepage Dashboard Card:")
        print(f"  Title:     {hp.get('title', app['name'].title())}")
        print(f"  Group:     {hp.get('group', 'Applications')}")
        print(f"  Icon:      {hp.get('icon', 'default.png')}")
        print(f"  Container: {hp.get('container', app['name'])}")


def cmd_resolve(args):
    """Resolve service name and output JSON for the bash caller."""
    if not args:
        sys.exit(1)
    query = args[0].strip()
    query_lower = query.lower()

    if query_lower == "core":
        print(json.dumps({
            "type": "core",
            "name": "core",
            "dir_path": CORE_DIR,
            "container": "",
            "domain": "internal",
            "has_compose": os.path.isfile(os.path.join(CORE_DIR, "docker-compose.yml")),
        }))
        return

    # Check if query matches a core infrastructure service
    for svc in get_core_services():
        if svc["name"].lower() == query_lower:
            print(json.dumps({
                "type": "core_service",
                "name": svc["name"],
                "dir_path": CORE_DIR,
                "container": svc["container"],
                "domain": svc["domain"],
                "has_compose": os.path.isfile(os.path.join(CORE_DIR, "docker-compose.yml")),
            }))
            return

    # Check applications under SITES_DIR
    app = resolve_app(query)
    if not app:
        sys.exit(1)
    app["type"] = "app"
    print(json.dumps(app))


def cmd_complete(args):
    """Output completions for shell autocomplete."""
    target = args[0] if args else "all"

    commands = [
        "list", "status", "ssl", "info", "up", "down",
        "restart", "update", "pull", "logs", "config", "sync", "completion",
    ]

    if target == "commands":
        print(" ".join(commands))
        return

    services = ["core"]
    for svc in get_core_services():
        services.append(svc["name"])

    for app in get_all_apps():
        services.append(app["name"])
        for alias in app.get("aliases", []):
            services.append(str(alias))

    if target == "services":
        print(" ".join(sorted(set(services))))
        return

    print(" ".join(sorted(set(commands + services))))


def cmd_sync_homepage(args):
    """Compile Sites/*/app.yaml into Core/config/homepage/services.yaml."""
    homepage_dir = os.path.join(CORE_DIR, "config", "homepage")
    for a in args:
        if a.startswith("--homepage-dir="):
            homepage_dir = a.split("=", 1)[1]
        elif a.startswith("--dashboard-dir="):
            homepage_dir = os.path.join(a.split("=", 1)[1], "config")

    services_yaml_path = os.path.join(homepage_dir, "services.yaml")
    if not os.path.isdir(os.path.dirname(services_yaml_path)):
        print(f"❌ Error: Homepage directory not found at {homepage_dir}")
        sys.exit(1)

    apps = get_all_apps()
    visible_apps = [a for a in apps if a.get("visible", True)]

    # Group by homepage group
    groups = {}

    # 1. Add Core Infrastructure services
    core_group = "Core Infrastructure"
    groups[core_group] = []
    core_metadata = {
        "traefik": {"title": "Traefik", "icon": "traefik.png", "weight": 10},
        "authelia": {"title": "Authelia", "icon": "authelia.png", "weight": 20},
        "lldap": {"title": "LLDAP Directory", "icon": "lldap.png", "weight": 25},
        "stalwart": {"title": "Stalwart Mail", "icon": "email.png", "weight": 26},
        "snappymail": {"title": "Webmail", "icon": "email.png", "weight": 27},
        "portainer": {"title": "Portainer", "icon": "portainer.png", "weight": 30},
        "dozzle": {"title": "Dozzle", "icon": "dozzle.png", "weight": 40},
    }
    for svc in get_core_services():
        name = svc["name"]
        if name in core_metadata and svc["domain"] != "internal":
            meta = core_metadata[name]
            groups[core_group].append({
                "title": meta["title"],
                "icon": meta["icon"],
                "href": f"https://{svc['domain']}",
                "description": svc["desc"],
                "server": "my-docker",
                "container": svc["container"],
                "weight": meta["weight"],
            })

    # 2. Add Site applications
    icons_dir = os.path.join(homepage_dir, "icons")
    for app in visible_apps:
        hp = app.get("homepage", {})
        group_name = hp.get("group", "Applications")
        if group_name not in groups:
            groups[group_name] = []

        card_title = hp.get("title", app["name"].title())
        icon = hp.get("icon", f"{app['name']}.png")
        container = hp.get("container", app["name"])
        weight = hp.get("weight", 50)
        desc = app.get("description", "")

        # Resolve custom local icons mounted in config/homepage/icons
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

        card_data = {
            "title": card_title,
            "icon": icon,
            "href": f"https://{app['domain']}",
            "description": desc,
            "server": "my-docker",
            "container": container,
            "weight": weight,
        }
        groups[group_name].append(card_data)

    # Preferred group order
    preferred_order = [
        "Core Infrastructure",
        "Knowledge & Notes",
        "Media & Productivity",
        "Development & AI",
        "Applications",
    ]
    sorted_groups = []
    for g in preferred_order:
        if g in groups:
            sorted_groups.append(g)
    for g in groups:
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
    with open(services_yaml_path, "w") as f:
        f.write(content)

    print(f"✨ Homepage synchronized successfully ({len(visible_apps)} visible apps in {len(sorted_groups)} groups)")
    print(f"📄 Generated {services_yaml_path}")


def main():
    if len(sys.argv) < 2:
        sys.exit(1)

    command = sys.argv[1]
    args = sys.argv[2:]

    if command == "list":
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
