"""``python3 -m adapters.codex`` — the codex sandbox shim.

Entry point baked into ``heg-sandbox-codex`` images: writes the codex
``config.toml`` (custom provider ``heg`` → gateway ``/v1/responses``), then
spawns ``codex exec --json`` and streams translated events.
"""

from __future__ import annotations

import os
import sys

from adapters.codex.config import write_config
from adapters.codex.translate import translate_obj
from adapters.common.runner import ShimConfig, build_task_text, run_shim

ARGV = [
    "codex",
    "exec",
    "--json",
    "--skip-git-repo-check",
    "--dangerously-bypass-approvals-and-sandbox",
]


def main() -> int:
    cfg = ShimConfig.from_env()
    argv = [*ARGV, build_task_text(cfg)]
    # The cred reaches codex via env_key=HEG_API_KEY in config.toml. The
    # adapter's env() sets it, but inject it here too so the image is
    # self-contained even if only GATEWAY_CRED was provided.
    child_env = dict(os.environ)
    child_env.setdefault("HEG_API_KEY", cfg.cred)
    return run_shim(
        harness="codex",
        cfg=cfg,
        argv=argv,
        translate=translate_obj,
        setup=write_config,
        env=child_env,
    )


if __name__ == "__main__":
    sys.exit(main())
