"""``python3 -m adapters.deepseek`` — the dsh sandbox shim.

Writes the headless-profile overlay, then spawns
``dsh --profile headless --json --permission-mode danger-full-access
--approval-policy never`` and streams translated events.
"""

from __future__ import annotations

import os
import sys

from adapters.common.runner import ShimConfig, build_task_text, run_shim
from adapters.deepseek.config import write_config
from adapters.deepseek.translate import translate_obj


def _argv(cfg: ShimConfig) -> list[str]:
    argv = [
        "dsh",
        "--profile",
        "headless",
        "--json",
        "--permission-mode",
        "danger-full-access",
        "--approval-policy",
        "never",
    ]
    argv.append(build_task_text(cfg))
    return argv


def main() -> int:
    cfg = ShimConfig.from_env()
    # The adapter's env() wires DEEPSEEK_API_KEY/DEEPSEEK_BASE_URL; inject
    # them here too so the image is self-contained when only GATEWAY_CRED /
    # GATEWAY_BASE_URL were provided (same pattern as codex's HEG_API_KEY).
    child_env = dict(os.environ)
    child_env.setdefault("DEEPSEEK_API_KEY", cfg.cred)
    child_env.setdefault("DEEPSEEK_BASE_URL", cfg.gateway_url)
    return run_shim(
        harness="deepseek",
        cfg=cfg,
        argv=_argv(cfg),
        translate=translate_obj,
        setup=write_config,
        env=child_env,
    )


if __name__ == "__main__":
    sys.exit(main())
