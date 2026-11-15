"""``python3 -m adapters.aider.launcher`` — the aider entrypoint, patched.

The shim spawns this module instead of the ``aider`` console script so one
upstream behavior can be adjusted in-process first.

aider-chat==0.86.2 resolves ``confirm_ask`` to ``"n"`` whenever a prompt
is flagged ``explicit_yes_required`` and ``yes`` is on — ``io.py``::
``res = "n" if explicit_yes_required else "y"``. The only such prompt is
"Run shell command?", so under ``--yes-always`` every model-suggested
shell command is declined — including the prompt footer's
``/usr/local/bin/done "…"`` suggestion, which made ``agent_done``
unreachable in the sandbox.

The explicit-yes gate protects *interactive* users from running commands
sight-unseen; the sandbox is headless by design and needs suggested
commands to run — that is how the agent invokes ``done``. The patch
clears the flag only while ``self.yes is True``, giving ``--yes-always``
its documented meaning ("Always say yes to every confirmation",
``args.py``) and leaving the interactive prompt path (``yes`` unset)
untouched.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any


def patched_confirm_ask(orig: Callable[..., bool]) -> Callable[..., bool]:
    """Wrap ``io.confirm_ask`` so ``--yes-always`` also confirms the
    ``explicit_yes_required`` prompts ("Run shell command?")."""

    def confirm_ask(
        self: Any,
        question: str,
        default: str = "y",
        subject: Any = None,
        explicit_yes_required: bool = False,
        group: Any = None,
        allow_never: bool = False,
    ) -> bool:
        if self.yes is True:
            explicit_yes_required = False
        return orig(
            self,
            question,
            default=default,
            subject=subject,
            explicit_yes_required=explicit_yes_required,
            group=group,
            allow_never=allow_never,
        )

    return confirm_ask


def main() -> Any:
    # Imports deferred so this module stays importable — and unit-testable —
    # on hosts without aider installed; it only runs inside the image.
    from aider.io import InputOutput
    from aider.main import main as aider_main

    InputOutput.confirm_ask = patched_confirm_ask(InputOutput.confirm_ask)
    # Same contract as the `aider` console script: argv comes from sys.argv
    # and the return value is the process exit status.
    return aider_main()


if __name__ == "__main__":
    sys.exit(main())
