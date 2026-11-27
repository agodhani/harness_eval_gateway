"""F0 smoke tests — the app boots and the health surface responds."""

import json
import shlex
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from fastapi.testclient import TestClient

COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"


def test_app_imports() -> None:
    from api.main import app  # noqa: F401


def test_health(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_compose_api_waits_for_gateway_to_listen() -> None:
    services = yaml.safe_load(COMPOSE.read_text())["services"]
    gateway = services["gateway"]

    assert services["api"]["depends_on"]["gateway"]["condition"] == "service_healthy"
    assert gateway["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert "api" not in gateway["depends_on"]
    assert not gateway["healthcheck"].get("disable", False)

    mode, command = gateway["healthcheck"]["test"]
    executable, flag, script = shlex.split(command)
    assert (mode, executable, flag) == ("CMD-SHELL", "python", "-c")
    stub = textwrap.dedent("""\
        import urllib.request
        from types import SimpleNamespace

        def urlopen(url, **kwargs):
            print(url)
            return SimpleNamespace(status=200)

        urllib.request.urlopen = urlopen
        """)
    probe = subprocess.run(
        [sys.executable, "-c", stub + script],
        capture_output=True, text=True, timeout=5, check=False,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.splitlines() == ["http://localhost:8080/health"]


@pytest.mark.docker
def test_compose_api_can_reach_delayed_gateway(tmp_path: Path) -> None:
    if not shutil.which("docker") or subprocess.run(
        ["docker", "info"], capture_output=True, timeout=30, check=False
    ).returncode:
        pytest.skip("Docker daemon unavailable")
    if subprocess.run(
        ["docker", "compose", "version"], capture_output=True, timeout=10, check=False
    ).returncode:
        pytest.skip("Docker Compose unavailable")
    services = json.loads(subprocess.check_output(
        ["docker", "compose", "-f", str(COMPOSE), "config", "--format", "json"],
        timeout=30,
    ))["services"]
    image = "python:3.12-slim"
    project = tmp_path / "compose.json"
    project.write_text(json.dumps({"services": {
        "postgres": {
            "image": image,
            "command": ["python", "-c", "from threading import Event; Event().wait()"],
            "healthcheck": {"test": ["CMD", "true"], "interval": "1s"},
        },
        "gateway": {
            "image": image,
            "depends_on": services["gateway"]["depends_on"],
            "healthcheck": services["gateway"].get("healthcheck", {}),
            "command": ["python", "-c", (
                "import os, time; from pathlib import Path; "
                "from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler; "
                "os.chdir('/tmp'); Path('health').write_text('ready'); time.sleep(3); "
                "ThreadingHTTPServer(('', 8080), SimpleHTTPRequestHandler).serve_forever()"
            )],
        },
        "api": {
            "image": image,
            "depends_on": services["api"]["depends_on"],
            "healthcheck": {"test": ["CMD", "true"], "interval": "1s"},
            "command": ["python", "-c", (
                "from urllib.request import urlopen; "
                "assert urlopen('http://gateway:8080/health', timeout=3).read() == b'ready'"
            )],
        },
    }}))
    compose = ["docker", "compose", "-p", f"heg-readiness-{uuid4().hex}", "-f", str(project)]
    try:
        proc = subprocess.run(
            [*compose, "up", "--exit-code-from", "api"],
            capture_output=True, text=True, timeout=120, check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
    finally:
        subprocess.run(
            [*compose, "down", "--timeout", "1"], capture_output=True, timeout=30, check=False
        )


def test_health_db(client: TestClient) -> None:
    resp = client.get("/health/db")
    if resp.status_code == 503:
        pytest.skip("database unavailable")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
