"""``python3 -m adapters.claude_code`` — the claude-code sandbox shim.

Seeds ~/.claude.json (onboarding completed, bypass accepted), then spawns
``claude -p --output-format stream-json --verbose
--dangerously-skip-permissions`` and streams translated events.
"""

from __future__ import annotations

import sys

from adapters.claude_code.config import write_config
from adapters.claude_code.translate import translate_obj
from adapters.common.runner import ShimConfig, build_task_text, run_shim


def _argv(cfg: ShimConfig) -> list[str]:
    argv = [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
    ]
    if cfg.model:
        argv += ["--model", cfg.model]
    if cfg.system_prompt_extra:
        argv += ["--append-system-prompt", cfg.system_prompt_extra.strip()]
    # system_prompt_extra is delivered natively — keep it out of the prompt.
    argv.append(build_task_text(cfg, extra_inline=False))
    return argv


def main() -> int:
    cfg = ShimConfig.from_env()
    return run_shim(
        harness="claude_code",
        cfg=cfg,
        argv=_argv(cfg),
        translate=translate_obj,
        setup=write_config,
    )


if __name__ == "__main__":
    sys.exit(main())
