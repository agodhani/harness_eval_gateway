"""Submit-time model-catalog validation (bug-hunt batch).

Best-effort checks against the resolved provider's model catalog — the same
gateway ``GET /v1/models`` path ``GET /api/models`` proxies — plus provider
capability metadata when the upstream reports one (Ollama ``/api/tags``
entries carry ``capabilities`` like ``"tools"``/``"vision"``).

- Model absent from the catalog → advisory warning on the create response
  (``RunOut.warnings``). Never a 422 — the name may be pullable or renamed
  upstream.
- Catalog reports capabilities and the model lacks ``"tools"`` while a
  tool-call context (harness adapter, or the miniloop eval judge) would run
  it → hard 422 — the same failure the sandbox dies with mid-run
  (``<model> does not support tools``), surfaced at submit instead.
- Catalog fetch failure / non-200 / malformed body → validation skipped
  entirely: submit keeps working offline.

One catalog fetch per provider per request, cached ~60s process-wide so a
burst of submits doesn't hammer the gateway (and one blip disables the
checks briefly rather than serially retrying).
"""

from __future__ import annotations

import fnmatch
import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from fastapi import HTTPException

if TYPE_CHECKING:
    from api.config import Settings
    from api.schemas import RunCreate

log = logging.getLogger(__name__)

_CATALOG_TTL_SECONDS = 60.0
_CATALOG_TIMEOUT = httpx.Timeout(10.0)

#: Registered adapters whose harness can run WITHOUT model tool/function
#: calls — aider falls back to litellm text edit-formats on no-tools models.
#: Every other adapter needs provider-reported ``"tools"`` support:
#: miniloop sends ``tools=`` on every chat call, and the CLIs' protocols
#: (anthropic-messages / responses / chat-completions) carry tools
#: unconditionally — the observed qwen2.5vl run died exactly this way. New
#: adapters default to tool-required; opt out only when the harness
#: provably works without function calling.
TOOL_OPTIONAL_ADAPTERS = frozenset({"aider"})

#: model id → capability frozenset; ``None`` when the provider doesn't
#: report per-model capabilities (tool check is then skipped for it).
Catalog = dict[str, frozenset[str] | None]

#: provider → (monotonic expiry, catalog-or-None-on-failure)
_CATALOG_CACHE: dict[str, tuple[float, Catalog | None]] = {}


def reset_catalog_cache() -> None:
    """Test/dev hook — drop every cached provider catalog."""
    _CATALOG_CACHE.clear()


@dataclass(frozen=True)
class _ModelUse:
    """One (label, model) pair the spec actually exercises."""

    label: str
    model: str
    needs_tools: bool
    context: str


def _provider_routes(raw: str) -> dict[str, str]:
    """Best-effort ``PROVIDER_ROUTES`` parse — invalid JSON degrades to
    ``{}`` (the gateway validates its own copy at boot; submit validation
    must never die on config it can't influence)."""
    try:
        parsed = json.loads((raw or "").strip() or "{}")
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(k): str(v) for k, v in parsed.items()}


def _provider_for_model(settings: Settings, model: str) -> str:
    """Mirror of ``gateway.config.Settings.provider_for_model`` —
    ``PROVIDER_ROUTES`` globs in declaration order, first match wins;
    local-name globs (no ``/``) never swallow a ``vendor/model`` id."""
    lowered = model.lower()
    namespaced = "/" in lowered
    for glob, provider in _provider_routes(
        getattr(settings, "PROVIDER_ROUTES", "") or ""
    ).items():
        pattern = glob.lower()
        if namespaced and "/" not in pattern:
            continue
        if fnmatch.fnmatchcase(lowered, pattern):
            return provider
    return getattr(settings, "UPSTREAM_PROVIDER", "ollama") or "ollama"


def _spec_models(body: RunCreate) -> list[_ModelUse]:
    """Every (label, model) the spec can execute, deduped.

    ``model_name`` + ``models[]`` run under ANY unpinned harness config, so
    they need tools when at least one configured adapter is tool-required.
    A per-config ``model`` pin only runs under that config's adapter. The
    evaluator model always needs tools — the eval judge is a miniloop
    sandbox, which sends ``tools=`` on every call.
    """
    out: list[_ModelUse] = []
    seen: set[tuple[str, str]] = set()

    def add(label: str, model: str | None, *, needs_tools: bool, context: str) -> None:
        m = (model or "").strip()
        if not m or (label, m) in seen:
            return
        seen.add((label, m))
        out.append(_ModelUse(label=label, model=m, needs_tools=needs_tools, context=context))

    tool_adapters = sorted(
        {hc.adapter for hc in body.harness_configs} - TOOL_OPTIONAL_ADAPTERS
    )
    participants_need_tools = bool(tool_adapters)
    context = f"configured harness adapter(s) {', '.join(tool_adapters)}"
    add(
        "model_name",
        body.model_name,
        needs_tools=participants_need_tools,
        context=context,
    )
    for m in body.models or []:
        add("models[]", m, needs_tools=participants_need_tools, context=context)
    for i, hc in enumerate(body.harness_configs):
        add(
            f"harness_configs[{i}].model",
            hc.model,
            needs_tools=hc.adapter not in TOOL_OPTIONAL_ADAPTERS,
            context=f"harness adapter {hc.adapter!r}",
        )
    add(
        "evaluator_model",
        body.evaluator_model or body.model_name,
        needs_tools=True,
        context="eval judge (miniloop)",
    )
    return out


async def _ollama_capabilities(
    settings: Settings, client: httpx.AsyncClient
) -> Catalog:
    """Ollama ``/api/tags`` → per-model ``capabilities``.

    The gateway's ``/v1/models`` shape doesn't carry capabilities, so the
    tool check reads the upstream's own metadata (the same Ollama the
    gateway forwards to — ``OLLAMA_BASE_URL`` is api-side config, no
    gateway change required). ``{}`` on any failure — capability data is
    optional; absence just skips the tool check.
    """
    base = (getattr(settings, "OLLAMA_BASE_URL", "") or "").strip()
    if not base:
        return {}
    try:
        resp = await client.get(f"{base.rstrip('/')}/api/tags")
    except httpx.HTTPError:
        log.info("modelcheck: ollama /api/tags unreachable", exc_info=True)
        return {}
    if resp.status_code != 200:
        return {}
    try:
        data = resp.json()
    except ValueError:
        return {}
    out: Catalog = {}
    for entry in data.get("models") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("model") or entry.get("name")
        caps = entry.get("capabilities")
        if isinstance(name, str) and name and isinstance(caps, list):
            out[name] = frozenset(str(c) for c in caps)
    return out


async def _fetch_catalog(settings: Settings, provider: str) -> Catalog | None:
    """Model catalog for ``provider`` via the gateway ``/v1/models`` path —
    the call ``GET /api/models`` proxies verbatim (service cred +
    ``x-heg-provider`` override). ``None`` on ANY fetch failure — callers
    treat it as "skip validation", never as "all models absent"."""
    # Deferred: routers import each other via FastAPI only at app assembly.
    from api.routers.models import _service_cred

    headers = {
        "Authorization": f"Bearer {_service_cred(settings.GATEWAY_CRED_SECRET)}",
        "x-heg-provider": provider,
    }
    async with httpx.AsyncClient(timeout=_CATALOG_TIMEOUT) as client:
        resp = await client.get(
            f"{settings.GATEWAY_URL.rstrip('/')}/v1/models", headers=headers
        )
        if resp.status_code != 200:
            log.info(
                "modelcheck: catalog for %s returned HTTP %s", provider, resp.status_code
            )
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        raw = data.get("data") if isinstance(data, dict) else None
        catalog: Catalog = {}
        for entry in raw if isinstance(raw, list) else []:
            if not isinstance(entry, dict):
                continue
            mid = entry.get("id")
            if not isinstance(mid, str) or not mid:
                continue
            caps = entry.get("capabilities")
            catalog[mid] = (
                frozenset(str(c) for c in caps) if isinstance(caps, list) else None
            )
        if provider == "ollama":
            catalog.update(await _ollama_capabilities(settings, client))
        return catalog


async def _catalog_for(settings: Settings, provider: str) -> Catalog | None:
    """Per-provider catalog, cached ~60s — hits AND misses (a gateway blip
    disables validation briefly rather than retrying per submit)."""
    entry = _CATALOG_CACHE.get(provider)
    if entry is not None and time.monotonic() < entry[0]:
        return entry[1]
    try:
        catalog = await _fetch_catalog(settings, provider)
    except Exception:
        log.info(
            "modelcheck: catalog fetch failed for %s — validation skipped",
            provider,
            exc_info=True,
        )
        catalog = None
    _CATALOG_CACHE[provider] = (time.monotonic() + _CATALOG_TTL_SECONDS, catalog)
    return catalog


async def validate_run_models(body: RunCreate, *, settings: Settings) -> list[str]:
    """Catalog-check every model the spec exercises; return advisory warnings.

    Hard 422 when the provider reports capabilities and the model lacks
    ``"tools"`` in a context that requires them — the message carries the
    provider's own capability report, mirroring the ollama error text the
    sandbox would die with (``<model> does not support tools``).
    """
    warnings: list[str] = []
    by_provider: dict[str, list[_ModelUse]] = {}
    for use in _spec_models(body):
        by_provider.setdefault(_provider_for_model(settings, use.model), []).append(use)
    for provider, uses in by_provider.items():
        catalog = await _catalog_for(settings, provider)
        if catalog is None:
            continue  # fetch failed — offline tolerance, no warnings
        for use in uses:
            caps = catalog.get(use.model)
            if use.model not in catalog:
                warnings.append(
                    f"{use.label} {use.model!r} is not in the {provider} "
                    "model catalog — the model may be pullable or renamed "
                    "upstream"
                )
            elif use.needs_tools and caps is not None and "tools" not in caps:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"{use.label} {use.model!r} does not support tools — "
                        f"{provider} reports capabilities {sorted(caps)}; the "
                        f"{use.context} issues tool calls the model would "
                        "reject mid-run"
                    ),
                )
    return warnings
