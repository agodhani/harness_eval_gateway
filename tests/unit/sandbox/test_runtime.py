from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock

import pytest

from sandbox.runtime import SandboxRuntime
from sandbox.spec import SandboxSpec


@pytest.mark.parametrize("uid", [1000, 1001])
@pytest.mark.parametrize("cached", [True, False])
def test_compose_spawn_gives_image_user_the_seeded_workspace(tmp_path, monkeypatch, uid, cached):
    runtime = SandboxRuntime(
        artifact_root=tmp_path / "artifacts",
        host_data_root=tmp_path,
    )
    workspace = tmp_path / "sandboxes" / "s1" / "workspace"
    seed = tmp_path / "seed"
    (seed / "src").mkdir(parents=True)
    (seed / "src" / "example.txt").write_text("hello")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "untouched.txt").touch()
    (seed / "link").symlink_to(outside, target_is_directory=True)
    changes: list[Path] = []
    commands: list[list[str]] = []

    def chown(path, owner, group, *, follow_symlinks):
        assert (owner, group, follow_symlinks) == (uid, uid, False)
        changes.append(path)

    def docker(args, **_kwargs):
        commands.append(args)
        if args[:2] == ["image", "inspect"]:
            return CompletedProcess(args, 0 if cached else 1, "", "")
        if args[0] == "pull":
            return CompletedProcess(args, 0, "", "")
        if args[1] == "--rm":
            return CompletedProcess(args, 0, f"uid={uid}(agent) gid={uid}(agent)", "")
        assert set(changes) == {
            workspace,
            workspace / "src",
            workspace / "src" / "example.txt",
            workspace / "link",
        }
        return CompletedProcess(args, 0, "container-id", "")

    monkeypatch.setattr("sandbox.runtime.os.geteuid", lambda: 0)
    monkeypatch.setattr("sandbox.runtime.os.chown", chown)
    monkeypatch.setattr(runtime, "ensure_network", Mock())
    monkeypatch.setattr(runtime, "_container_exists", lambda _name: False)
    monkeypatch.setattr(runtime, "_docker", docker)

    result = runtime.spawn(SandboxSpec(
        sandbox_id="s1",
        run_id="r1",
        image="heg-sandbox:test",
        workspace_host_path=workspace,
        seed_host_path=seed,
    ))

    assert result.seed_sha
    assert commands[0] == ["image", "inspect", "heg-sandbox:test"]
    owner_probe = 1 if cached else 2
    if not cached:
        assert commands[1] == ["pull", "heg-sandbox:test"]
    assert commands[owner_probe] == [
        "run", "--rm", "--pull", "never", "--network", "none",
        "--entrypoint", "id", "heg-sandbox:test",
    ]
    assert commands[owner_probe + 1][0:2] == ["run", "-d"]
    assert (workspace / "src" / "example.txt").read_text() == "hello"
