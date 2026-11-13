"""F10 artifact-preview unit tests — detection, jail resolution, manager.

``detect_preview`` / ``resolve_workspace_dir`` are pure filesystem — exercised
against tmp_path workspace shapes. ``PreviewManager`` is tested with a stubbed
docker CLI (``_docker`` on a fake runtime) so spawn/list/stop/reap logic runs
hermetically; the real spawn path is covered by tests/integration/test_preview.py
(``@pytest.mark.docker``).
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from sandbox.preview import (
    CONTAINER_PORT,
    DESKTOP_ARTIFACT_ROOT,
    DESKTOP_CONTAINER_PORT,
    DESKTOP_NETWORK,
    PREVIEW_NETWORK,
    SERVE_ROOT,
    Preview,
    PreviewManager,
    PreviewPlan,
    detect_preview,
    resolve_workspace_dir,
)

# ---------------------------------------------------------------------------
# detect_preview — workspace shapes
# ---------------------------------------------------------------------------


def test_detect_missing_and_empty(tmp_path: Path) -> None:
    assert detect_preview(tmp_path / "nope") is None
    assert detect_preview(tmp_path) is None
    (tmp_path / "app.py").write_text("print(1)")
    (tmp_path / "README.md").write_text("# x")
    assert detect_preview(tmp_path) is None


def test_detect_root_index(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<h1>hi</h1>")
    (tmp_path / "style.css").write_text("")
    plan = detect_preview(tmp_path)
    assert plan == PreviewPlan(type="static", entry="index.html")


def test_detect_index_wins_over_other_root_html(tmp_path: Path) -> None:
    (tmp_path / "index.htm").write_text("i")
    (tmp_path / "about.html").write_text("a")
    plan = detect_preview(tmp_path)
    assert plan is not None and plan.entry == "index.htm"


def test_detect_single_root_html(tmp_path: Path) -> None:
    (tmp_path / "game.html").write_text("<canvas>")
    plan = detect_preview(tmp_path)
    assert plan == PreviewPlan(type="static", entry="game.html")


def test_detect_ambiguous_root_html(tmp_path: Path) -> None:
    (tmp_path / "a.html").write_text("a")
    (tmp_path / "b.html").write_text("b")
    assert detect_preview(tmp_path) is None


def test_detect_nested_index(tmp_path: Path) -> None:
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "index.html").write_text("i")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x")
    plan = detect_preview(tmp_path)
    assert plan == PreviewPlan(type="static", entry="dist/index.html")


def test_detect_ambiguous_nested_index(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "index.html").write_text("a")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "index.html").write_text("b")
    assert detect_preview(tmp_path) is None


def test_detect_skips_hidden_dirs(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "index.html").write_text("git internals")
    assert detect_preview(tmp_path) is None


def test_detect_ignores_symlinked_entry(tmp_path: Path, tmp_path_factory) -> None:
    outside = tmp_path_factory.mktemp("outside")
    secret = outside / "secret.html"
    secret.write_text("secret")
    (tmp_path / "index.html").symlink_to(secret)
    assert detect_preview(tmp_path) is None


def test_detect_ignores_symlinked_dir(tmp_path: Path, tmp_path_factory) -> None:
    outside = tmp_path_factory.mktemp("outside")
    (outside / "index.html").write_text("outside")
    (tmp_path / "site").symlink_to(outside, target_is_directory=True)
    assert detect_preview(tmp_path) is None


# ---------------------------------------------------------------------------
# detect_preview — desktop (pygame) workspaces
# ---------------------------------------------------------------------------


def test_detect_desktop_main_py_imports_pygame(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("import pygame\npygame.init()\n")
    plan = detect_preview(tmp_path)
    assert plan == PreviewPlan(type="desktop", entry="main.py")


def test_detect_desktop_from_pygame(tmp_path: Path) -> None:
    (tmp_path / "game.py").write_text("from pygame import sprite\n")
    plan = detect_preview(tmp_path)
    assert plan == PreviewPlan(type="desktop", entry="game.py")


def test_detect_desktop_single_py(tmp_path: Path) -> None:
    (tmp_path / "thing.py").write_text("import pygame\n")
    plan = detect_preview(tmp_path)
    assert plan == PreviewPlan(type="desktop", entry="thing.py")


def test_detect_desktop_requirements_evidence(tmp_path: Path) -> None:
    """requirements.txt mentioning pygame-ce counts even without an import."""
    (tmp_path / "main.py").write_text("import engine\n")  # thin launcher
    (tmp_path / "requirements.txt").write_text("pygame-ce==2.5.3\n")
    plan = detect_preview(tmp_path)
    assert plan == PreviewPlan(type="desktop", entry="main.py")


def test_detect_desktop_evidence_in_sibling_py(tmp_path: Path) -> None:
    """Entry needn't import pygame itself — any root .py counts."""
    (tmp_path / "main.py").write_text("import engine\n")
    (tmp_path / "engine.py").write_text("import pygame\n")
    plan = detect_preview(tmp_path)
    assert plan == PreviewPlan(type="desktop", entry="main.py")


def test_detect_desktop_no_evidence(tmp_path: Path) -> None:
    """A bare main.py is NOT executed — fail safe on missing pygame signal."""
    (tmp_path / "main.py").write_text("print('hi')\n")
    assert detect_preview(tmp_path) is None


def test_detect_desktop_no_entry(tmp_path: Path) -> None:
    """Pygame evidence without an unambiguous entry → not previewable."""
    (tmp_path / "engine.py").write_text("import pygame\n")
    (tmp_path / "sprites.py").write_text("import pygame\n")
    assert detect_preview(tmp_path) is None


def test_detect_desktop_comment_does_not_count(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("# import pygame  # was used before\n")
    assert detect_preview(tmp_path) is None


def test_detect_desktop_symlinked_entry(tmp_path: Path, tmp_path_factory) -> None:
    outside = tmp_path_factory.mktemp("outside")
    (outside / "main.py").write_text("import pygame\n")
    (tmp_path / "main.py").symlink_to(outside / "main.py")
    assert detect_preview(tmp_path) is None


def test_detect_static_wins_over_desktop(tmp_path: Path) -> None:
    """index.html + main.py → static (safe default, no guessing/executing)."""
    (tmp_path / "index.html").write_text("<h1>x</h1>")
    (tmp_path / "main.py").write_text("import pygame\n")
    plan = detect_preview(tmp_path)
    assert plan == PreviewPlan(type="static", entry="index.html")


def test_detect_desktop_nested_py_not_found(tmp_path: Path) -> None:
    """Nested entry points aren't auto-detected (root-level only)."""
    (tmp_path / "game").mkdir()
    (tmp_path / "game" / "main.py").write_text("import pygame\n")
    assert detect_preview(tmp_path) is None


# ---------------------------------------------------------------------------
# resolve_workspace_dir — artifact-root jail
# ---------------------------------------------------------------------------


def _ws(jail: Path, run: str = "r1", sb: str = "s1") -> Path:
    ws = jail / run / sb / "workspace"
    ws.mkdir(parents=True)
    return ws


def test_resolve_conventional_layout(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    got = resolve_workspace_dir(
        jail=tmp_path, run_id="r1", sandbox_id="s1", artifact_path=None
    )
    assert got == ws.resolve()


def test_resolve_via_artifact_path(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    got = resolve_workspace_dir(
        jail=tmp_path,
        run_id="other",
        sandbox_id="s1",
        artifact_path=str(tmp_path / "r1" / "s1"),
    )
    assert got == ws.resolve()


def test_resolve_artifact_path_outside_jail(tmp_path: Path) -> None:
    got = resolve_workspace_dir(
        jail=tmp_path, run_id="r1", sandbox_id="s1", artifact_path="/etc"
    )
    assert got is None


def test_resolve_traversal_blocked(tmp_path: Path) -> None:
    outside = tmp_path.parent / "ws-outside"
    (outside / "workspace").mkdir(parents=True, exist_ok=True)
    try:
        got = resolve_workspace_dir(
            jail=tmp_path,
            run_id="r1",
            sandbox_id="s1",
            artifact_path=str(tmp_path / ".." / "ws-outside"),
        )
        assert got is None
    finally:
        (outside / "workspace").rmdir()
        outside.rmdir()


def test_resolve_symlinked_workspace_blocked(tmp_path: Path, tmp_path_factory) -> None:
    """A candidate-planted `workspace` symlink escaping the jail → None."""
    real = tmp_path_factory.mktemp("real")
    ws = tmp_path / "r1" / "s1" / "workspace"
    ws.parent.mkdir(parents=True)
    ws.symlink_to(real, target_is_directory=True)
    got = resolve_workspace_dir(
        jail=tmp_path, run_id="r1", sandbox_id="s1", artifact_path=None
    )
    assert got is None


def test_resolve_missing_workspace(tmp_path: Path) -> None:
    (tmp_path / "r1" / "s1").mkdir(parents=True)
    assert (
        resolve_workspace_dir(
            jail=tmp_path, run_id="r1", sandbox_id="s1", artifact_path=None
        )
        is None
    )


# ---------------------------------------------------------------------------
# PreviewManager — stubbed docker CLI
# ---------------------------------------------------------------------------


class FakeRuntime:
    """Duck-typed SandboxRuntime stand-in: records docker calls, replies canned."""

    def __init__(
        self, *, inspect_rows: list[dict] | None = None, internal: bool = False
    ) -> None:
        self.calls: list[list[str]] = []
        self.inspect_rows = inspect_rows or []
        # inspect routing by container id — needed when runner + proxy differ
        self.rows_by_id: dict[str, dict] = {}
        self.ps_ids: list[str] = []
        self.proxy_ps_ids: list[str] = []  # ps filtered on heg.preview_role=proxy
        # per-preview_id ps routing: {pid: [ids]} — falls back to ps_ids
        self.ps_by_preview_id: dict[str, list[str]] = {}
        self.run_container_id = "cid123"
        self.networks: set[str] = set()
        self.images: set[str] = set()
        self.internal = internal
        self.run_calls = 0
        self.fail_run_at: int | None = None  # raise on the Nth `docker run`

    def to_host_path(self, path: str | Path) -> Path:
        return Path(path)

    def _docker(
        self,
        args: list[str],
        *,
        check: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        rc, out, err = 0, "", ""
        if args[:2] == ["network", "inspect"]:
            rc = 0 if args[2] in self.networks else 1
            out = json.dumps([{"Internal": self.internal}]) if rc == 0 else ""
        elif args[:2] == ["network", "create"]:
            self.networks.add(args[-1])
        elif args[:2] == ["network", "connect"]:
            pass
        elif args[:2] == ["image", "inspect"]:
            rc = 0 if args[2] in self.images else 1
        elif args[:2] == ["ps", "-aq"]:
            filters = [args[i + 1] for i, a in enumerate(args) if a == "--filter"]
            if any("heg.preview_role=proxy" in f for f in filters):
                out = "\n".join(self.proxy_ps_ids)
            elif any(f.startswith("name=") for f in filters):
                out = ""  # name-collision probe — fresh names never exist
            else:
                pid_filter = next(
                    (
                        f.removeprefix("label=heg.preview_id=")
                        for f in filters
                        if f.startswith("label=heg.preview_id=")
                    ),
                    None,
                )
                if pid_filter is not None and pid_filter in self.ps_by_preview_id:
                    out = "\n".join(self.ps_by_preview_id[pid_filter])
                else:
                    out = "\n".join(self.ps_ids)
        elif args[0] == "inspect" and len(args) > 1:
            if self.rows_by_id:
                out = json.dumps(
                    [self.rows_by_id[i] for i in args[1:] if i in self.rows_by_id]
                )
            else:
                out = json.dumps(self.inspect_rows)
        elif args[0] == "run":
            self.run_calls += 1
            if self.fail_run_at is not None and self.run_calls == self.fail_run_at:
                rc = 1
                err = "simulated run failure"
            else:
                out = self.run_container_id + "\n"
        elif args[0] == "rm":
            pass
        if check and rc != 0:
            raise RuntimeError(f"docker {' '.join(args)} failed")
        return subprocess.CompletedProcess(args, rc, out, err)


def _inspect_row(
    *,
    container_id: str = "cid123",
    name: str = "/heg-prev-abc",
    status: str = "running",
    port: str | None = "49152",
    labels: dict[str, str] | None = None,
    created: str = "2026-01-01T00:00:00.000000000Z",
) -> dict:
    return {
        "Id": container_id,
        "Name": name,
        "Created": created,
        "State": {"Status": status},
        "Config": {"Labels": labels or {}},
        "NetworkSettings": {
            "Ports": (
                {f"{CONTAINER_PORT}/tcp": [{"HostIp": "127.0.0.1", "HostPort": port}]}
                if port
                else {}
            )
        },
    }


def _labels(**over: str) -> dict[str, str]:
    base = {
        "heg.preview": "1",
        "heg.preview_id": "11111111-1111-1111-1111-111111111111",
        "heg.run_id": "r1",
        "heg.sandbox_id": "s1",
        "heg.preview_type": "static",
        "heg.preview_entry": "index.html",
        "heg.preview_expires_at": f"{time.time() + 900:.0f}",
    }
    base.update(over)
    return base


def _manager(fake: FakeRuntime, **kw) -> PreviewManager:
    return PreviewManager(runtime=fake, **kw)  # type: ignore[arg-type]


def test_ensure_network_creates_dedicated_bridge() -> None:
    """NOT --internal: published ports are dropped on internal networks."""
    fake = FakeRuntime()
    _manager(fake).ensure_network()
    assert PREVIEW_NETWORK in fake.networks
    create = next(c for c in fake.calls if c[:2] == ["network", "create"])
    assert "--internal" not in create
    assert "com.docker.network.bridge.enable_ip_masquerade=false" in create


def test_ensure_network_idempotent() -> None:
    fake = FakeRuntime()
    fake.networks.add(PREVIEW_NETWORK)
    _manager(fake).ensure_network()
    assert not [c for c in fake.calls if c[:2] == ["network", "create"]]


def test_ensure_network_warns_when_existing_is_internal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An internal heg-preview can't publish ports — loud warning required."""
    import logging

    fake = FakeRuntime(internal=True)
    fake.networks.add(PREVIEW_NETWORK)
    with caplog.at_level(logging.WARNING, logger="sandbox.preview"):
        _manager(fake).ensure_network()
    assert any("internal" in r.message.lower() for r in caplog.records)


def test_spawn_args_and_preview(tmp_path: Path) -> None:
    fake = FakeRuntime(
        inspect_rows=[_inspect_row(labels=_labels())]
    )
    fake.ps_ids = ["cid123"]  # label query finds the just-spawned container
    mgr = _manager(fake, image="img:test", bind_host="127.0.0.1", ttl_seconds=60)
    preview = mgr.spawn(
        run_id="r1",
        sandbox_id="s1",
        serve_dir=tmp_path,
        plan=PreviewPlan(type="static", entry="index.html"),
    )
    run = next(c for c in fake.calls if c[0] == "run")
    assert run[run.index("--network") + 1] == PREVIEW_NETWORK
    assert run[run.index("-p") + 1] == f"127.0.0.1::{CONTAINER_PORT}"
    mount = run[run.index("-v") + 1]
    assert mount == f"{tmp_path}:{SERVE_ROOT}:ro"
    for flag in ("--read-only", "--cap-drop", "--security-opt", "--user"):
        assert flag in run
    assert run[run.index("--user") + 1] == "65534:65534"
    label_args = [run[i + 1] for i, v in enumerate(run) if v == "--label"]
    assert "heg.preview=1" in label_args
    assert any(a.startswith("heg.preview_id=") for a in label_args)
    assert any(a.startswith("heg.preview_expires_at=") for a in label_args)
    # image + http.server command tail
    assert "img:test" in run
    tail = run[run.index("img:test") :]
    assert tail[:4] == ["img:test", "python", "-m", "http.server"]
    assert str(CONTAINER_PORT) in tail and SERVE_ROOT in tail

    assert preview.port == 49152
    assert preview.url == "http://localhost:49152/"
    assert preview.status == "running"
    assert preview.live


def test_spawn_rejects_unknown_type(tmp_path: Path) -> None:
    fake = FakeRuntime()
    mgr = _manager(fake)
    with pytest.raises(ValueError, match="unsupported preview type"):
        mgr.spawn(
            run_id="r1",
            sandbox_id="s1",
            serve_dir=tmp_path,
            plan=PreviewPlan(type="python-server", entry="app.py"),
        )
    assert not [c for c in fake.calls if c[0] == "run"]


PID_A = "11111111-1111-1111-1111-111111111111"
PID_B = "22222222-2222-2222-2222-222222222222"
PID_OLD = "33333333-3333-3333-3333-333333333333"
PID_NEW = "44444444-4444-4444-4444-444444444444"


def test_list_rebuilds_registry_from_labels() -> None:
    future = f"{time.time() + 100:.0f}"
    fake = FakeRuntime(
        inspect_rows=[
            _inspect_row(
                container_id="a",
                name="/heg-prev-a",
                labels=_labels(**{"heg.preview_id": PID_A}),
            ),
            _inspect_row(
                container_id="b",
                name="/heg-prev-b",
                status="exited",
                port=None,
                created="2026-01-02T00:00:00.000000000Z",
                labels=_labels(
                    **{
                        "heg.preview_id": PID_B,
                        "heg.preview_entry": "dist/index.html",
                        "heg.preview_expires_at": future,
                    }
                ),
            ),
            _inspect_row(container_id="c", name="/other", labels={}),
        ]
    )
    fake.ps_ids = ["a", "b", "c"]
    previews = _manager(fake).list()
    assert [p.preview_id for p in previews] == [PID_B, PID_A]  # newest first
    a, b = previews[1], previews[0]
    assert a.url == "http://localhost:49152/"
    assert a.expires_at is not None
    assert b.status == "exited" and not b.live and b.url is None


def test_list_skips_malformed_preview_id() -> None:
    fake = FakeRuntime(
        inspect_rows=[
            _inspect_row(labels=_labels(**{"heg.preview_id": "not-a-uuid"})),
        ]
    )
    fake.ps_ids = ["a"]
    assert _manager(fake).list() == []


def test_get_for_sandbox_filters(tmp_path: Path) -> None:
    fake = FakeRuntime(
        inspect_rows=[_inspect_row(labels=_labels())]
    )
    fake.ps_ids = ["a"]
    mgr = _manager(fake)
    got = mgr.get_for_sandbox("s1")
    assert got is not None and got.sandbox_id == "s1"
    ps = next(c for c in fake.calls if c[:2] == ["ps", "-aq"])
    assert "label=heg.sandbox_id=s1" in ps
    assert "label=heg.preview=1" in ps


def test_stop_removes_by_preview_label() -> None:
    fake = FakeRuntime()
    fake.ps_ids = ["deadbeef"]
    mgr = _manager(fake)
    assert mgr.stop("pid-1") is True
    rm = next(c for c in fake.calls if c[0] == "rm")
    assert rm == ["rm", "-f", "deadbeef"]
    ps = next(c for c in fake.calls if c[:2] == ["ps", "-aq"])
    assert "label=heg.preview_id=pid-1" in ps


def test_stop_missing_returns_false() -> None:
    fake = FakeRuntime()
    assert _manager(fake).stop("nope") is False


def test_reap_expired_only_removes_past_ttl() -> None:
    expired = _labels(
        **{
            "heg.preview_id": PID_OLD,
            "heg.preview_expires_at": f"{time.time() - 10:.0f}",
        }
    )
    fresh = _labels(
        **{
            "heg.preview_id": PID_NEW,
            "heg.preview_expires_at": f"{time.time() + 600:.0f}",
        }
    )
    fake = FakeRuntime(
        inspect_rows=[
            _inspect_row(container_id="old", name="/heg-prev-old", labels=expired),
            _inspect_row(container_id="new", name="/heg-prev-new", labels=fresh),
        ]
    )
    fake.ps_ids = ["old", "new"]
    # stop(PID_OLD) queries by preview_id label — only "old" matches.
    fake.ps_by_preview_id[PID_OLD] = ["old"]
    fake.ps_by_preview_id[PID_NEW] = ["new"]
    mgr = _manager(fake)
    removed = mgr.reap_expired()
    assert removed == 1
    rm = next(c for c in fake.calls if c[0] == "rm")
    assert rm == ["rm", "-f", "old"]


def test_expiry_falls_back_to_created_plus_ttl() -> None:
    labels = _labels()
    del labels["heg.preview_expires_at"]
    fake = FakeRuntime(inspect_rows=[_inspect_row(labels=labels)])
    fake.ps_ids = ["a"]
    preview = _manager(fake, ttl_seconds=60).list()[0]
    # created 2026-01-01 + 60s — safely in the past, so the reaper would reap it
    assert preview.expires_at is not None
    assert preview.expires_at.timestamp() < time.time()


def test_preview_url_variants() -> None:
    from sandbox.preview import _preview_url

    assert _preview_url(None, "index.html") is None
    assert _preview_url(8000, None) == "http://localhost:8000/"
    assert _preview_url(8000, "index.html") == "http://localhost:8000/"
    assert _preview_url(8000, "dist/index.html") == "http://localhost:8000/dist/"
    assert _preview_url(8000, "game.html") == "http://localhost:8000/game.html"


def test_preview_dataclass_live_property() -> None:
    base = {
        "preview_id": "p",
        "run_id": "r",
        "sandbox_id": "s",
        "type": "static",
        "container": "c",
        "port": 1,
        "entry": "index.html",
        "url": "u",
        "created_at": None,
        "expires_at": None,
    }
    assert Preview(status="running", **base).live
    assert not Preview(status="exited", **base).live


# ---------------------------------------------------------------------------
# PreviewManager — desktop spawn (runner on internal net + socat sidecar)
# ---------------------------------------------------------------------------

PID_DESK = "55555555-5555-5555-5555-555555555555"


def _desktop_fake() -> FakeRuntime:
    """Runner row (no published port) + proxy row (publishes 6080)."""
    runner = _inspect_row(
        container_id="cid123",
        name="/heg-prev-dead",
        port=None,
        labels=_labels(
            **{
                "heg.preview_id": PID_DESK,
                "heg.preview_type": "desktop",
                "heg.preview_entry": "main.py",
                "heg.preview_role": "runner",
            }
        ),
    )
    proxy = _inspect_row(
        container_id="cidproxy",
        name="/heg-prev-dead-proxy",
        port=None,
        labels=_labels(
            **{
                "heg.preview_id": PID_DESK,
                "heg.preview_type": "desktop",
                "heg.preview_role": "proxy",
            }
        ),
    )
    proxy["NetworkSettings"] = {
        "Ports": {
            f"{DESKTOP_CONTAINER_PORT}/tcp": [
                {"HostIp": "127.0.0.1", "HostPort": "49555"}
            ]
        }
    }
    fake = FakeRuntime()
    fake.rows_by_id = {"cid123": runner, "cidproxy": proxy}
    fake.ps_ids = ["cid123", "cidproxy"]  # both share the preview_id label
    fake.proxy_ps_ids = ["cidproxy"]
    fake.images.add("heg-preview-desktop:latest")
    return fake


def test_spawn_desktop_runner_args(tmp_path: Path) -> None:
    fake = _desktop_fake()
    mgr = _manager(
        fake,
        desktop_cpus="1.5",
        desktop_memory="256m",
        desktop_pids=64,
    )
    mgr.spawn(
        run_id="r1",
        sandbox_id="s1",
        serve_dir=tmp_path,
        plan=PreviewPlan(type="desktop", entry="main.py"),
    )
    runs = [c for c in fake.calls if c[0] == "run"]
    assert len(runs) == 2
    runner, proxy = runs[0], runs[1]

    # Runner: internal net, NO published port, resource bounds, :ro mount.
    assert runner[runner.index("--network") + 1] == DESKTOP_NETWORK
    assert "-p" not in runner
    assert runner[runner.index("--cpus") + 1] == "1.5"
    assert runner[runner.index("--memory") + 1] == "256m"
    assert runner[runner.index("--pids-limit") + 1] == "64"
    assert runner[runner.index("-v") + 1] == f"{tmp_path}:{DESKTOP_ARTIFACT_ROOT}:ro"
    envs = [runner[i + 1] for i, v in enumerate(runner) if v == "-e"]
    assert "APP_ENTRY=main.py" in envs
    for flag in ("--cap-drop", "--security-opt", "--user"):
        assert flag in runner
    labels = [runner[i + 1] for i, v in enumerate(runner) if v == "--label"]
    assert "heg.preview_role=runner" in labels
    assert "heg.preview_type=desktop" in labels
    assert "heg-preview-desktop:latest" in runner

    # Internal network ensured (--internal create).
    connect = next(c for c in fake.calls if c[:2] == ["network", "connect"])
    assert connect[2] == DESKTOP_NETWORK
    creates = [c for c in fake.calls if c[:2] == ["network", "create"]]
    desk_create = next(c for c in creates if DESKTOP_NETWORK in c)
    assert "--internal" in desk_create

    # Proxy: publishable net + socat pipe args, proxy role label.
    assert proxy[proxy.index("--network") + 1] == PREVIEW_NETWORK
    assert proxy[proxy.index("-p") + 1] == f"127.0.0.1::{DESKTOP_CONTAINER_PORT}"
    assert "alpine/socat:1.8.1.3" in proxy
    tail = proxy[proxy.index("alpine/socat:1.8.1.3") :]
    assert f"TCP-LISTEN:{DESKTOP_CONTAINER_PORT},fork,reuseaddr" in tail
    assert tail[-1].startswith("TCP:heg-prev-") and tail[-1].endswith(":6080")
    plabels = [proxy[i + 1] for i, v in enumerate(proxy) if v == "--label"]
    assert "heg.preview_role=proxy" in plabels


def test_spawn_desktop_resolves_port_and_url(tmp_path: Path) -> None:
    fake = _desktop_fake()
    preview = _manager(fake).spawn(
        run_id="r1",
        sandbox_id="s1",
        serve_dir=tmp_path,
        plan=PreviewPlan(type="desktop", entry="main.py"),
    )
    assert preview.type == "desktop"
    assert preview.port == 49555
    assert preview.url == "http://localhost:49555/vnc.html?autoconnect=true&resize=scale"
    assert preview.live


def test_spawn_desktop_missing_image(tmp_path: Path) -> None:
    fake = FakeRuntime()  # no images
    mgr = _manager(fake)
    with pytest.raises(RuntimeError, match="not found"):
        mgr.spawn(
            run_id="r1",
            sandbox_id="s1",
            serve_dir=tmp_path,
            plan=PreviewPlan(type="desktop", entry="main.py"),
        )
    assert not [c for c in fake.calls if c[0] == "run"]


def test_spawn_desktop_proxy_failure_cleans_runner(tmp_path: Path) -> None:
    fake = _desktop_fake()
    fake.fail_run_at = 2  # runner runs, proxy fails
    mgr = _manager(fake)
    with pytest.raises(RuntimeError):
        mgr.spawn(
            run_id="r1",
            sandbox_id="s1",
            serve_dir=tmp_path,
            plan=PreviewPlan(type="desktop", entry="main.py"),
        )
    rm = next(c for c in fake.calls if c[0] == "rm")
    assert rm[:2] == ["rm", "-f"]
    assert len(rm) == 4  # runner + proxy names


def test_row_to_preview_skips_proxy_rows() -> None:
    proxy = _inspect_row(
        labels=_labels(**{"heg.preview_role": "proxy", "heg.preview_id": PID_DESK})
    )
    fake = FakeRuntime(inspect_rows=[proxy])
    fake.ps_ids = ["x"]
    assert _manager(fake).list() == []


def test_get_desktop_preview_inspects_both_ids() -> None:
    fake = _desktop_fake()
    fake.ps_ids = ["cid123", "cidproxy"]  # runner + proxy under one preview_id
    preview = _manager(fake).get(PID_DESK)
    assert preview is not None
    assert preview.type == "desktop" and preview.port == 49555


def test_stop_removes_all_labeled_containers() -> None:
    fake = FakeRuntime()
    fake.ps_ids = ["runner", "proxy"]
    assert _manager(fake).stop("pid") is True
    rm = next(c for c in fake.calls if c[0] == "rm")
    assert rm == ["rm", "-f", "runner", "proxy"]


def test_desktop_url_shape() -> None:
    from sandbox.preview import _preview_url

    assert (
        _preview_url(9999, "main.py", "desktop")
        == "http://localhost:9999/vnc.html?autoconnect=true&resize=scale"
    )
