"""Aider plain-text status lines → normalized event dicts.

Aider has no machine-readable stream: ``aider --message`` prints a human
transcript (``--no-pretty --no-stream`` keeps it line-stable). The shim
feeds each non-JSON stdout line here — ``translate_obj`` drops every
parsed dict because aider emits no JSON records of its own, and command
output (``cat report.json``) must not inject forged events.

Line → event mapping (aider-chat 0.86.x strings — ``base_coder.py`` /
``repo.py`` / ``commands.py`` / ``io.py``)::

    Aider v0.86.2                                checkpoint(session)
    Main model: openai/x with diff edit format checkpoint(model)
    Weak model: openai/x                         checkpoint(weak_model)
    Git repo: .git with 12 files / none          checkpoint(git_repo)
    Git repository created in /workspace         checkpoint(git_init)
    Repo-map: using 1024 tokens, auto refresh    checkpoint(repo_map)
    Added f.py to the chat [(read-only)]         checkpoint(file_added)
    Added 12 lines of output to the chat.        checkpoint(context_output)
    Applied edit to f.py                         file_op(op=edit)
    Skipping edits to f.py [gitignore spec]      checkpoint(edit_skipped)
    The LLM did not conform to the edit format.  error(edit_format, recoverable)
    Committing f.py before applying edits.       checkpoint(pre_commit)
    Commit 1a2b3c4 aider: msg                    checkpoint(commit)
    Unable to commit: ... / Unable to ...        error(commit|aider)
    Running pytest -q                            shell_command
    Tokens: 4.0k sent, 89 received. [Cost: …]    checkpoint(llm_usage)
    Cost: $0.01 message, $0.02 session.          checkpoint(cost)
    Retrying in 0.5 seconds...                   checkpoint(llm_retry)
    litellm.APIConnectionError: ...              error(llm)
    Traceback (most recent call last):           error(crash)
    Model x has hit a token limit!               error(token_limit)
    Starting to summarize chat history.          checkpoint(summarize_start)
    Finished summarizing chat history.           context_compacted
    Warmed 2k cached tokens.                     checkpoint(cache_warm)
    Run shell command? y   (confirm echo)        checkpoint(confirm)

Everything else — assistant prose, command output, SEARCH/REPLACE block
text, spinner residue — maps to ``[]``; the raw stderr transcript keeps
it all.

Runs inside the sandbox — stdlib only.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from adapters.common.runner import MAX_SNIPPET


def _truncate(value: Any, limit: int = MAX_SNIPPET) -> str:
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= limit else text[:limit] + "…[truncated]"


def _checkpoint(stage: str, **fields: Any) -> dict[str, Any]:
    return {"type": "checkpoint", "payload": {"stage": stage, **fields}}


def _error(stage: str, message: Any, **fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"stage": stage, "message": _truncate(message, 2000)}
    payload.update(fields)
    return {"type": "error", "payload": payload}


def _toknum(text: str | None) -> int | None:
    """``format_tokens`` values → ints: ``999`` / ``4.0k`` / ``12k`` / ``1,234``."""
    if not text:
        return None
    t = text.strip().rstrip(".").replace(",", "")
    mult = 1
    if t.lower().endswith("k"):
        mult = 1000
        t = t[:-1]
    try:
        return int(float(t) * mult)
    except ValueError:
        return None


def _file_added(match: re.Match[str], line: str) -> list[dict[str, Any]]:
    return [
        _checkpoint(
            "file_added",
            path=match.group(1),
            read_only=bool(match.group(2)),
        )
    ]


def _file_added_ro(match: re.Match[str], line: str) -> list[dict[str, Any]]:
    return [_checkpoint("file_added", path=match.group(1), read_only=True)]


def _applied_edit(match: re.Match[str], line: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "file_op",
            "payload": {"op": "edit", "path": match.group(1), "tool": "aider"},
        }
    ]


def _skipped_dry_run(match: re.Match[str], line: str) -> list[dict[str, Any]]:
    return [_checkpoint("edit_skipped", path=match.group(1), reason="dry_run")]


def _skipped_edit(match: re.Match[str], line: str) -> list[dict[str, Any]]:
    return [
        _checkpoint(
            "edit_skipped",
            path=match.group(1),
            reason="gitignore" if match.group(2) else None,
        )
    ]


def _commit(match: re.Match[str], line: str) -> list[dict[str, Any]]:
    return [
        _checkpoint(
            "commit",
            sha=match.group(1),
            message=_truncate(match.group(2), 500) if match.group(2) else None,
        )
    ]


def _shell_command(match: re.Match[str], line: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "shell_command",
            "payload": {"command": _truncate(match.group(1), 2000), "tool": "run"},
        }
    ]


def _llm_usage(match: re.Match[str], line: str) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {
        "sent": _toknum(match.group(1)),
        "cache_write": _toknum(match.group(2)),
        "cache_hit": _toknum(match.group(3)),
        "received": _toknum(match.group(4)),
        "text": _truncate(line, 500),
    }
    if match.group(5):
        payload["cost_message"] = match.group(5)
        payload["cost_session"] = match.group(6)
    return [_checkpoint("llm_usage", **payload)]


def _model_line(match: re.Match[str], line: str) -> list[dict[str, Any]]:
    return [
        _checkpoint(
            "main_model",
            model=match.group(1),
            edit_format=match.group(2),
        )
    ]


def _named_model_line(match: re.Match[str], line: str) -> list[dict[str, Any]]:
    role = match.group(1).lower()
    return [_checkpoint(f"{role}_model", model=match.group(2))]


def _context_output(match: re.Match[str], line: str) -> list[dict[str, Any]]:
    return [_checkpoint("context_output", lines=int(match.group(1)))]


def _confirm(match: re.Match[str], line: str) -> list[dict[str, Any]]:
    return [
        _checkpoint(
            "confirm",
            question=match.group(1),
            response=match.group(2),
        )
    ]


def _unable_commit(match: re.Match[str], line: str) -> list[dict[str, Any]]:
    return [_error("commit", match.group(1) or line)]


def _unable_generic(match: re.Match[str], line: str) -> list[dict[str, Any]]:
    return [_error("aider", line)]


def _conform_error(match: re.Match[str], line: str) -> list[dict[str, Any]]:
    # Aider retries the turn — a real LLM conformance failure, not a fatal one.
    return [_error("edit_format", line, recoverable=True)]


def _litellm_error(match: re.Match[str], line: str) -> list[dict[str, Any]]:
    return [_error("llm", line)]


def _traceback(match: re.Match[str], line: str) -> list[dict[str, Any]]:
    return [_error("crash", line)]


def _token_limit(match: re.Match[str], line: str) -> list[dict[str, Any]]:
    return [_error("token_limit", line, model=match.group(1))]


def _context_compacted(match: re.Match[str], line: str) -> list[dict[str, Any]]:
    return [{"type": "context_compacted", "payload": {"trigger": "chat_history"}}]


def _cache_warm(match: re.Match[str], line: str) -> list[dict[str, Any]]:
    return [_checkpoint("cache_warm", tokens=_toknum(match.group(1)))]


def _stage(stage: str, key: str | None = None) -> Callable[[re.Match[str], str], list[dict[str, Any]]]:
    def _go(match: re.Match[str], line: str) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"text": _truncate(line, 500)}
        if key:
            payload[key] = match.group(1)
        return [_checkpoint(stage, **payload)]

    return _go


#: Ordered (pattern, handler) rules — first match wins. Specific ``Unable to``
#: variants must precede the generic fallback.
_RULES: list[tuple[re.Pattern[str], Callable[[re.Match[str], str], list[dict[str, Any]]]]] = [
    (re.compile(r"^Aider v(\S+)"), _stage("session", "version")),
    (re.compile(r"^Main model: (\S+)(?: with (\S+) edit format)?"), _model_line),
    (re.compile(r"^(Weak|Editor) model: (\S+)"), _named_model_line),
    (re.compile(r"^Git repository created in (.+)"), _stage("git_init", "path")),
    (re.compile(r"^Git repo: (.+)"), _stage("git_repo")),
    (re.compile(r"^Repo-map: (.+)"), _stage("repo_map")),
    (
        re.compile(r"^Added (\d+) lines? of output to the chat"),
        _context_output,
    ),
    (
        re.compile(r"^Added clipboard image to the chat: (.+)"),
        _stage("file_added", "path"),
    ),
    (re.compile(r"^Added (.+?) to the chat( \(read-only\))?\.?$"), _file_added),
    (re.compile(r"^Added (.+?) to read-only files\.?$"), _file_added_ro),
    (re.compile(r"^Applied edit to (.+)"), _applied_edit),
    (
        re.compile(r"^Did not apply edit to (.+?)( \(--dry-run\))?\.?$"),
        _skipped_dry_run,
    ),
    (
        re.compile(r"^Skipping edits to (.+?)( that matches gitignore spec)?\.?$"),
        _skipped_edit,
    ),
    (re.compile(r"^The LLM did not conform to the edit format\.?"), _conform_error),
    (
        re.compile(r"^Committing (.+?) before applying edits\.?$"),
        _stage("pre_commit", "path"),
    ),
    (re.compile(r"^Commit ([0-9a-f]{7,40})(?: (.+))?$"), _commit),
    (re.compile(r"^Unable to commit: ?(.*)$"), _unable_commit),
    (re.compile(r"^Running (.+)$"), _shell_command),
    (
        re.compile(
            r"^Tokens: (\S+) sent"
            r"(?:, (\S+) cache write)?"
            r"(?:, (\S+) cache hit)?"
            r", (\S+) received\."
            r"(?:\s+Cost: \$(\S+) message, \$(\S+) session\.)?"
        ),
        _llm_usage,
    ),
    (re.compile(r"^Cost: (.+)"), _stage("cost")),
    (
        re.compile(r"^Retrying in ([\d.]+) seconds"),
        _stage("llm_retry", "delay_s"),
    ),
    (re.compile(r"^litellm\."), _litellm_error),
    (re.compile(r"^Traceback \(most recent call last\)"), _traceback),
    (re.compile(r"^Model (\S+) has hit a token limit"), _token_limit),
    (
        re.compile(r"^Starting to summarize chat history"),
        _stage("summarize_start"),
    ),
    (
        re.compile(r"^Finished summarizing chat history"),
        _context_compacted,
    ),
    (re.compile(r"^Warmed (\S+) cached tokens"), _cache_warm),
    (
        # confirm_ask echoes "<question>? <res>" under --yes-always — covers
        # "Run shell command? y", "Create new file? y", "Add … to the chat?",
        # "Add .aider* to .gitignore (recommended)? y", lint/test fix asks.
        re.compile(r"^(.+\?) (y|yes|n|no|a|s|d)$"),
        _confirm,
    ),
    (re.compile(r"^Unable to (.+)$"), _unable_generic),
]


def translate_text(line: str) -> list[dict[str, Any]]:
    """One aider stdout line → normalized event dicts ([] when unmatched)."""
    text = line.strip()
    if not text:
        return []
    for pattern, handler in _RULES:
        match = pattern.search(text)
        if match:
            try:
                return handler(match, text)
            except Exception:  # noqa: BLE001 — a bad line yields nothing
                return []
    return []


def translate_obj(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Aider emits no JSON records — every parsed dict is dropped.

    This is load-bearing, not a stub: JSON-looking stdout lines can only be
    echoed command output (``cat data.json``), and translating them would
    let the candidate's own files forge harness events.
    """
    return []


def translate_events(lines: Any) -> list[dict[str, Any]]:
    """Convenience wrapper used by tests — adapter.py routes through
    ``contract.cli_translate_events`` with the same translate pair plus
    HEG_RESULT marker folding."""
    from adapters.common.contract import cli_translate_events

    return cli_translate_events(lines, translate_obj, translate_text)
