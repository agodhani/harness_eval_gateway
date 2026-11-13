"""``python3 -m adapters.pi`` — the pi-agent sandbox shim.

Writes ``~/.pi/agent/models.json`` (provider ``heg`` → gateway
``/v1/chat/completions``), then spawns ``pi --mode json --provider heg
--model <model>`` and streams translated events.
"""

from __future__ import annotations

import sys

from adapters.common.runner import ShimConfig, build_task_text, run_shim
from adapters.pi.config import write_config
from adapters.pi.translate import translate_obj


def _argv(cfg: ShimConfig) -> list[str]:
    argv = ["pi", "--mode", "json", "--no-session"]
    if cfg.model:
        argv += ["--provider", "heg", "--model", cfg.model]
    if cfg.cred:
        argv += ["--api-key", cfg.cred]
    if cfg.system_prompt_extra:
        argv += ["--append-system-prompt", cfg.system_prompt_extra.strip()]
    # system_prompt_extra is delivered natively — keep it out of the prompt.
    argv.append(build_task_text(cfg, extra_inline=False))
    return argv


def main() -> int:
    cfg = ShimConfig.from_env()
    return run_shim(
        harness="pi",
        cfg=cfg,
        argv=_argv(cfg),
        translate=translate_obj,
        setup=write_config,
    )


if __name__ == "__main__":
    sys.exit(main())
