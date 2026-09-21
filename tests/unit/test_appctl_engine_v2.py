import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Import appctl engine v2
CORE_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(CORE_ROOT / "scripts"))

import appctl_engine_v2 as engine


@dataclass
class AppFactory:
    """Helper class to generate mock app directories and configurations"""
    sites_dir: Path

    def create(
        self,
        dir_name: str,
        manifest_yaml: str = "",
        has_compose: bool = True,
    ) -> Path:
        app_dir: Path = self.sites_dir / dir_name
        app_dir.mkdir(parents=True, exist_ok=True)
        if manifest_yaml:
            (app_dir / "app.yaml").write_text(manifest_yaml, encoding="utf-8")
        if has_compose:
            (app_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        return app_dir

@pytest.fixture
def mock_sites_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fixture that creates an isolated Sites directory and points engine.SITES_DIR to it"""
    sites = tmp_path / "Sites"
    monkeypatch.setattr(engine, "SITES_DIR", str(sites))
    monkeypatch.setattr(engine, "HOMELAB_DOMAIN", "mydomain.test")
    return sites

@pytest.fixture
def app_factory(mock_sites_dir: Path) -> AppFactory:
    """Fixture that provides the AppFactory instance to test"""
    return AppFactory(mock_sites_dir)

@pytest.fixture
def mock_core_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Fixture that creates an isolated Core directory and points engine.CORE_DIR to it"""
    core = tmp_path / "CORE"
    core.mkdir(parents=True, exist_ok=True)
    return core

## GREEN TEST: Existing implementation should not break

def test_resolve_app_hierarchy(app_factory: AppFactory):
    """Ensure resolution follows: Canonical Name > Alias > Dir Name > Prefix"""
    app_factory.create(
        dir_name="homelab-jellyfin",
        manifest_yaml="""
        name: media-server
        aliases:
            - movies
        """,
    )
    apps = engine.get_sites_apps()

    assert engine.resolve_app("media-server", apps) is not None
    assert engine.resolve_app("movies", apps) is not None
    assert engine.resolve_app("homelab-jellyfin", apps) is not None
    assert engine.resolve_app("unknown", apps) is None

def test_manifest_data_overrides_default_values(app_factory: AppFactory):
    """Ensure app.yaml values successfully override the system defaults"""
    app_factory.create(
        dir_name="homelab-custom",
        manifest_yaml="""
        name: custom-app
        domain: custom.roadtotech.local
        auth: false
        visible: false
        networks:
            - custom-net
        """
    )

    apps = engine.get_sites_apps()
    app = engine.resolve_app("custom-app", apps)

    assert app is not None
    assert app.domain == "custom.roadtotech.local"
    assert app.auth is False
    assert app.networks == ["custom-net"]

def test_sites_app_normalization(app_factory: AppFactory):
    """Ensure missing or badly typed manifest fields are safely normalized"""
    app_factory.create(
        dir_name="homelab-custom",
        manifest_yaml="""
        name: testapp
        # Missing domain - should fallback to <name>.roadtotech.me
        aliases: single-string-alias # should become a list[str]
        auth: "False" # should evaluate based on boolean casting
        homepage:
            weight: "99"    # this should be casted as an int
        """
    )
    apps = engine.get_sites_apps()
    app = engine.resolve_app("testapp", apps)

    assert app is not None
    assert app.name == "testapp"
    assert app.domain == "testapp.mydomain.test"
    assert app.aliases == ["single-string-alias"]
    assert app.auth is True
    assert app.homepage is not None
    assert app.homepage.weight == 99


## RED TESTS: TDD Goals for next implementation

@pytest.mark.xfail(reason="TDD in progress: get_docker_status not yet implemented")
def test_docker_status_parser_running(monkeypatch):
    """
    TDD Goal: Port `get_docker_status` to V2
    It must shell out to `docker-compse ps` and `docker inspect`
    then return the correct UI badge
    """
    def mock_run(args, **kwargs):
        """Mocking a running"""
        mock = MagicMock()
        if "ps" in args:
            mock.stdout = "container123\n"
        if "inspect" in args:
            mock.stdout = "true\n"
        return mock

    monkeypatch.setattr("subprocess.run", mock_run)

    status = engine.get_docker_status("/fake/dir")
    assert status == "🟢 Running (1)"

@pytest.mark.xfail(reason="TDD in progress: cmd_sync_homepage not yet implemented")
def test_homepage_sync_groups_and_sorts_correctly(
    mock_core_dir: Path,
    app_factory: AppFactory,
    monkeypatch):
    """
    TDD Goal: Port `cmd_sync_homepage` to V2 using the new Dataclass structure.
    It must group by `homepage.group` and sort by `homepage.weight`.
    """
    hp_config_dir = mock_core_dir / "config" / "homepage"
    hp_config_dir.mkdir(parents=True, exist_ok=True)

    app_factory.create(
        dir_name="homelab-app1",
        manifest_yaml="""
        name: App1
        domain: app1.local.test
        homepage:
            title: App1
            group: Group B
            weight: 20
        """
    )

    app_factory.create(
        dir_name="homelab-app2",
        manifest_yaml="""
        name: App2
        domain: app2.local.test
        homepage:
            title: App2
            group: Group A
            weight: 10
        """
    )

    monkeypatch.setattr(engine, "get_core_services", list)
    engine.cmd_sync_homepage(["--homepage-dir=" + str(hp_config_dir)])

    output_file = hp_config_dir / "services.yaml"
    assert output_file.exists(), "The services.yaml file must be generated"

    content = output_file.read_text()
    assert content.find("- Group A") < content.find("- Group B") # Group A should appear before Group B
    assert "App2" in content # App 2 is in the file
