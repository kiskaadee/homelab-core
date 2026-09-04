#!/usr/bin/env python3
"""
appctl_engine.py - Homelab Metadata Engine, Orchestration Parser & Git Sync Monitor
"""

import os
import sys
import json
import subprocess
import shutil
from concurrent.futures import ThreadPoolExecutor

SITES_DIR = os.environ.get("SITES_DIR", os.path.expanduser("~/Sites"))
CORE_DIR = os.environ.get("CORE_DIR", os.path.expanduser("~/Core"))
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
            try:
                manifest = parse_yaml_simple(open(manifest_path).read())
            except Exception:
                pass

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
    except Exception:
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
    except Exception:
        return "❓ Unknown"


def get_git_diagnostics(dir_path):
    """Get detailed git diagnostics for info command."""
    if not os.path.isdir(os.path.join(dir_path, ".git")):
        return None

    diag = {}
    try:
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
    except Exception:
        pass
    return diag


def fetch_repository(dir_path):
    """Run git fetch on a single repository with timeout."""
    if os.path.isdir(os.path.join(dir_path, ".git")):
        try:
            subprocess.run(
                ["git", "fetch", "--quiet"],
                cwd=dir_path,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except Exception:
            pass


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
        {"name": "portainer", "domain": "portainer.roadtotech.me", "container": "portainer", "desc": "Container Management GUI"},
        {"name": "dozzle", "domain": "logs.roadtotech.me", "container": "dozzle", "desc": "Real-time Log Viewer"},
        {"name": "socket-proxy", "domain": "internal", "container": "socket-proxy", "desc": "Docker Socket Security Proxy"},
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
        except Exception:
            svc["status"] = "❓ Unknown"
    return core_services


def cmd_list(args):
    """Format and print application and core stack listings with Git sync status."""
    show_core = any(a in ["--core", "-c", "--all", "-a"] for a in args)
    should_fetch = any(a in ["--fetch", "-f"] for a in args)
    apps = get_all_apps()

    if should_fetch:
        print("📡 Fetching remote updates across all repositories...")
        fetch_all_repositories(apps, include_core=show_core)

    print(f"{'SERVICE':<16} {'STATUS':<15} {'GIT SYNC':<15} {'DOMAIN':<28} {'DIRECTORY'}")
    print(f"{'-------':<16} {'------':<15} {'--------':<15} {'------':<28} {'---------'}")

    for app in apps:
        status = get_docker_status(app["dir_path"])
        git_status = get_git_sync_status(app["dir_path"])
        rel_dir = app["dir_path"].replace(os.path.expanduser("~"), "~")
        print(f"{app['name']:<16} {status:<15} {git_status:<15} {app['domain']:<28} {rel_dir}")

    if show_core:
        core_git = get_git_sync_status(CORE_DIR)
        print()
        print(f"{'CORE SERVICE':<16} {'STATUS':<15} {'GIT SYNC':<15} {'DOMAIN':<28} {'DIRECTORY'}")
        print(f"{'------------':<16} {'------':<15} {'--------':<15} {'------':<28} {'---------'}")
        core_services = get_core_services()
        rel_core = CORE_DIR.replace(os.path.expanduser("~"), "~")
        for i, svc in enumerate(core_services):
            # Print core repo git status on the first row
            row_git = core_git if i == 0 else ""
            print(f"{svc['name']:<16} {svc['status']:<15} {row_git:<15} {svc['domain']:<28} {rel_core}")


def cmd_info(args):
    """Show detailed metadata, runtime overview, and Git diagnostics for a service."""
    if not args:
        print("❌ Error: Service name required for 'info' command")
        sys.exit(1)

    query = args[0]
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
    app = resolve_app(args[0])
    if not app:
        sys.exit(1)
    print(json.dumps(app))


def cmd_sync_homepage(args):
    """Compile Sites/*/app.yaml into homelab-dashboard/config/services.yaml."""
    dashboard_dir = os.path.join(SITES_DIR, "homelab-dashboard")
    for a in args:
        if a.startswith("--dashboard-dir="):
            dashboard_dir = a.split("=", 1)[1]

    services_yaml_path = os.path.join(dashboard_dir, "config", "services.yaml")
    if not os.path.isdir(os.path.dirname(services_yaml_path)):
        print(f"❌ Error: Dashboard directory not found at {dashboard_dir}")
        sys.exit(1)

    apps = get_all_apps()
    visible_apps = [a for a in apps if a.get("visible", True)]

    # Group by homepage group
    groups = {}
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
    preferred_order = ["Knowledge & Notes", "Media & Productivity", "Development & AI", "Applications"]
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
    elif command == "info":
        cmd_info(args)
    elif command == "resolve":
        cmd_resolve(args)
    elif command == "sync":
        cmd_sync_homepage(args)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
