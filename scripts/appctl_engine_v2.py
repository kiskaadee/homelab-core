import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Dynamic Environment Paths
CORE_DIR = os.environ.get(
    "CORE_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)

SITES_DIR = os.environ.get(
    "SITES_DIR",
    os.path.join(os.path.dirname(CORE_DIR), "Sites")
    if os.path.isdir(os.path.join(os.path.dirname(CORE_DIR), "Sites"))
    else os.path.expanduser("~/Sites"),
)


HOMELAB_DOMAIN = os.environ.get("HOMELAB_DOMAIN") or os.environ.get("DOMAIN", "")


def validate_environment() -> None:
    """Ensure mandatory environment configuration is valid before CLI execution."""
    errors: list[str] = []

    if not HOMELAB_DOMAIN:
        errors.append(
            "• Missing 'HOMELAB_DOMAIN' (or 'DOMAIN') environment variable.\n"
            "  Please export it (e.g. export HOMELAB_DOMAIN='roadtotech.me') or set it in your environment file."
        )

    if not os.path.isdir(CORE_DIR):
        errors.append(f"• Core directory not found at: {CORE_DIR}")

    if errors:
        print("❌ Environment Validation Failed:\n", file=sys.stderr)
        for err in errors:
            print(f"  {err}", file=sys.stderr)
        sys.exit(1)

class ManifestError(Exception):
    pass

@dataclass
class HomepageConfig:
    title: str
    group: str = "Applications"
    icon: str = "default.png"
    container: str = ""
    weight: int = 50

@dataclass
class App:
    name: str                       # Canonical identified (e.g., "traefik", "jellyfin")
    description: str                # Human-readable summary for dashboards and CLI info
    domain: str                     # Routing FQDN (e.g., "auth.roadtotech.me") or "internal".
    dir_path: str                   # Filesystem directory (CORE_DIR for Core, or ~/Sites/<app> for Sites).

    @property
    def is_internal(self) -> bool:
        """Returns True if the service does not expose a public TLS endpoint."""
        return not self.domain or self.domain == "internal" or self.domain.endswith(".local") or ":" in self.domain

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON consumption by scripts/appctl"""
        data = asdict(self)
        data["type"] = getattr(self, "app_type", "app")
        return data

@dataclass
class CoreService(App):
    container: str = ""             # Target container name inside Core's root docker-compose.yml (e.g., "authelia", "stalwart")
    weight: int = 50                # Homepage ordering weight (optional, for dashboard synchronization)
    icon: str = "default.png"       # Dashboard icon filename
    app_type: str = "core_service"
    dir_path: str = CORE_DIR

@dataclass
class SitesApp(App):
    dir_name: str = ""              # directory name in ~/Sites (e.g., "homelab-vaultwarden")
    aliases: list[str] = field(default_factory=list)    # CLI resolution aliases (e.g., "vw", "vaultwarden")
    visible: bool = True            # Defines whether the icon is visible in the Homepage Dashboard
    auth: bool = True               # Authelia protection guard indicator; Defaulting to True is safer
    has_compose: bool = False       # Whether a docker-compose.yaml exists at ~/Sites/dir_name.
    networks: list[str] = field(default_factory=lambda: ["proxy-net"])  # Docker network attachments (def ["proxy-net"])
    env: dict[str, Any] = field(default_factory=dict)   # Dynamic environment variable overrides
    homepage: HomepageConfig | None = None              # Dashboard Card Configuration
    manifest_error: str | None = None                   # Captured syntax/loading errors if app.yaml was malformed/absent
    app_type: str = "app"



def load_manifest(manifest_path: str) -> dict[str, Any]:
    """Load and parese the app manifest file

    Raises:
        FileNotFoundError: if the manifest file does not exist.
        ValueError: if YAML object is malformed.
        TypeError: If root is not a directory
    """
    if not os.path.isfile(manifest_path):
        raise FileNotFoundError(f"Manifest file not found at {manifest_path}")

    try:
        with open(manifest_path, "r", encoding="utf-8") as file:
            manifest_data = yaml.safe_load(file) or {}
    except (yaml.YAMLError, OSError) as err:
        raise ManifestError(f"YAML parsing failed: {err}") from err

    if not isinstance(manifest_data, dict):
        raise ManifestError(f"Invalid manifest root: expected `dict`, got `{type(manifest_data).__name__}`")

    return manifest_data



def sites_app_from_manifest(

    app_dir: str,
    manifest: dict[str, Any],
    manifest_error: str | None = None
) -> SitesApp:
    """Construct and normalize a SitesApp domain model from raw manifest data."""
    raw_dir_name = os.path.basename(app_dir)
    canonical_name = str(manifest.get("name") or raw_dir_name.replace("homelab-", ""))

    # Normalize aliases into list[str]
    raw_aliases = manifest.get("aliases") or []
    if isinstance(raw_aliases, str):
        aliases = [raw_aliases]
    elif isinstance(raw_aliases, list):
        aliases = [str(alias) for alias in raw_aliases]
    else:
        aliases = []

    # Homepage card normalization
    homepage_config: HomepageConfig | None = None
    raw_hp = manifest.get("homepage")
    if isinstance(raw_hp, dict):
        homepage_config = HomepageConfig(
            title=str(raw_hp.get("title") or canonical_name),
            group=str(raw_hp.get("group") or "Applications"),
            icon=str(raw_hp.get("icon") or "default.png"),
            container=str(raw_hp.get("container") or canonical_name),
            weight=int(raw_hp.get("weight", 50))
        )
    has_compose = (
        os.path.isfile(os.path.join(app_dir, "docker-compose.yaml")) or
        os.path.isfile(os.path.join(app_dir, "docker-compose.yml"))
    )

    # Manifest Data parsing
    raw_desc = manifest.get("description")
    app_desc: str = str(raw_desc) if raw_desc is not None else ""

    raw_dom = manifest.get("domain")
    app_dom = str(raw_dom) if raw_dom is not None else f"{canonical_name}.{HOMELAB_DOMAIN}"

    app_net = manifest.get("networks")
    if not isinstance(app_net, list):
        app_net = ["proxy-net"]

    app_env = manifest.get("env")
    if not isinstance(app_env, dict):
        app_env = {}

    def _as_bool(value: Any, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        return default

    app_vis = _as_bool(manifest.get("visible"), True)
    app_auth = _as_bool(manifest.get("auth"), True)

    return SitesApp(
        name=canonical_name,
        dir_name=raw_dir_name,
        description=app_desc,
        domain=app_dom,
        dir_path=app_dir,
        has_compose=has_compose,
        aliases=aliases,
        visible=app_vis,
        auth=app_auth,
        networks=app_net,
        env=app_env,
        homepage= homepage_config,
        manifest_error=manifest_error,
    )



def get_sites_apps(sites_dir: str | None = None) -> list[SitesApp]:
    """
    Scan the given sites directory (defaulting to SITES_DIR) for application.
    """
    target_dir = sites_dir or SITES_DIR
    apps: list[SitesApp] = []
    if not os.path.isdir(target_dir):
        return apps

    for entry in sorted(os.listdir(target_dir)):
        app_dir = os.path.join(target_dir, entry)
        # skip hidden or null directories
        if entry.startswith(".") or not os.path.isdir(app_dir):
            continue
        manifest_path = os.path.join(app_dir, "app.yaml")
        manifest: dict[str, Any] = {}
        manifest_error: str | None = None

        if os.path.isfile(manifest_path):
            try:
                manifest = load_manifest(manifest_path=manifest_path)
            except ManifestError as err:
                manifest_error = str(err)
        apps.append(sites_app_from_manifest(app_dir, manifest, manifest_error))

    return apps

def resolve_app(
    query: str,
    apps: list[SitesApp] | None = None,
    sites_dir: str | None = None
) -> SitesApp | None:

    if apps is None:
        apps = get_sites_apps(sites_dir= sites_dir if sites_dir is not None else SITES_DIR)

    query_clean = query.strip().lower()

    # 1. Extract match on canonical name
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

    return None

def get_core_services(core_dir: str | None = None) -> list[CoreService]:
    """Return the strongly-typed registry of Core platform services."""
    return [
        CoreService(
            name="traefik",
            domain=f"traefik.{HOMELAB_DOMAIN}",
            container="traefik",
            description="Edge Reverse Proxy & ACME TLS",
            icon="traefik.png",
            weight=10,
        ),
        CoreService(
            name="authelia",
            domain=f"auth.{HOMELAB_DOMAIN}",
            container="authelia",
            description="Identity & SSO Access Control",
            icon="authelia.png",
            weight=20,
        ),
        CoreService(
            name="lldap",
            domain=f"users.{HOMELAB_DOMAIN}",
            container="lldap",
            description="Lightweight LDAP User Directory",
            icon="lldap.png",
            weight=25,
        ),
        CoreService(
            name="stalwart",
            domain=f"mail.{HOMELAB_DOMAIN}",
            container="stalwart",
            description="All-in-one Mail Server",
            icon="email.png",
            weight=26,
        ),
        CoreService(
            name="snappymail",
            domain=f"webmail.{HOMELAB_DOMAIN}",
            container="snappymail",
            description="Modern Lightweight Webmail Client",
            icon="email.png",
            weight=27,
        ),
        CoreService(
            name="portainer",
            domain=f"portainer.{HOMELAB_DOMAIN}",
            container="portainer",
            description="Container Management GUI",
            icon="portainer.png",
            weight=30,
        ),
        CoreService(
            name="dozzle",
            domain=f"logs.{HOMELAB_DOMAIN}",
            container="dozzle",
            description="Real-time Log Viewer",
            icon="dozzle.png",
            weight=40,
        ),
        CoreService(
            name="socket-proxy",
            domain="internal",
            container="socket-proxy",
            description="Docker Socket Security Proxy",
        ),
        CoreService(
            name="homepage",
            domain=f"dashboard.{HOMELAB_DOMAIN}",
            container="homepage",
            description="Application Dashboard & System Portal",
            icon="homepage.png",
            weight=5,
        ),
        CoreService(
            name="diun",
            domain="internal",
            container="diun",
            description="Docker Image Update Notifier",
        ),
        CoreService(
            name="watchtower",
            domain="internal",
            container="watchtower",
            description="Automated Container Updates",
        ),
    ]


def get_docker_status(
    dir_path: str | Path,
    container_name: str | None = None
) -> str:
    path: Path = Path(dir_path)
    def _do_something(path: Path = path):
        return ""
    return _do_something()

def cmd_sync_homepage(args) -> None:
    pass
