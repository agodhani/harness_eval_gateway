"""``python3 -m adapters.aider`` — the aider sandbox shim.

Writes ``$HOME/.aider.conf.yml`` (openai provider → gateway
``/v1/chat/completions``, headless flags, relocated history files), then
spawns ``aider --yes-always --message "<task>"`` and translates its
plain-text status lines via ``translate.py``.
"""

from __future__ import annotations

import os
import sys

from adapters.aider.config import write_config
from adapters.aider.translate import translate_obj, translate_text
from adapters.common.runner import ShimConfig, build_task_text, run_shim


def _argv(cfg: ShimConfig) -> list[str]:
    # Every steady flag lives in the conf file; argv is just the invocation.
    return ["aider", "--yes-always", "--message", build_task_text(cfg)]


def main() -> int:
    cfg = ShimConfig.from_env()
    # The adapter's env() wires OPENAI_API_KEY/OPENAI_API_BASE; inject them
    # here too so the image is self-contained when only GATEWAY_CRED /
    # GATEWAY_BASE_URL were provided (same pattern as codex's HEG_API_KEY).
    # GIT_* identity lets aider's auto-commits land in the repo it creates
    # under /workspace (raw `git commit` needs author+committer).
    child_env = dict(os.environ)
    child_env.setdefault("OPENAI_API_KEY", cfg.cred)
    child_env.setdefault("OPENAI_API_BASE", f"{cfg.gateway_url}/v1")
    child_env.setdefault("OPENAI_BASE_URL", f"{cfg.gateway_url}/v1")
    child_env.setdefault("GIT_AUTHOR_NAME", "aider")
    child_env.setdefault("GIT_AUTHOR_EMAIL", "aider@heg.local")
    child_env.setdefault("GIT_COMMITTER_NAME", "aider")
    child_env.setdefault("GIT_COMMITTER_EMAIL", "aider@heg.local")
    return run_shim(
        harness="aider",
        cfg=cfg,
        argv=_argv(cfg),
        translate=translate_obj,
        translate_text=translate_text,
        setup=write_config,
        env=child_env,
    )


if __name__ == "__main__":
    sys.exit(main())
