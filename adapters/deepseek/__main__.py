"""``python3 -m adapters.deepseek`` — the dsh sandbox shim.

Writes the headless-profile overlay, then spawns
``dsh --profile headless --json --permission-mode danger-full-access
--approval-policy never`` and streams translated events.
"""

from __future__ import annotations

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
    return run_shim(
        harness="deepseek",
        cfg=cfg,
        argv=_argv(cfg),
        translate=translate_obj,
        setup=write_config,
    )


if __name__ == "__main__":
    sys.exit(main())
