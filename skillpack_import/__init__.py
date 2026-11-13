"""skillpack_import — user-imported skill packs from GitHub (F10).

Imported packs are USER DATA: they live under ``data/skillpacks/`` (derived
from ``ARTIFACT_ROOT.parent`` — the same data tree as artifacts and sandbox
state), NOT in the vendored ``skillpacks/`` tree, which is committed to git
and baked into the api image in compose (``COPY . .`` — writes there would
be ephemeral). Both roots feed pack listing (``GET /api/skillpacks``),
submit-time validation, and provisioning staging — a selected imported pack
lands at ``/opt/skillpacks/<pack>`` in the sandbox exactly like a vendored
one.

Fetch strategy: codeload repo tarballs
(``https://codeload.github.com/<owner>/<repo>/tar.gz/<ref>``) — one request
yields the whole tree, so unauthenticated ``api.github.com`` rate limits
(60/hr) never apply. ``HEAD`` resolves the default branch; ambiguous refs
from ``/tree/<branch>/<path>`` URLs (branch names may contain slashes) are
resolved by probing candidate refs longest-first, mirroring GitHub's own
longest-match behavior.

Private repos: unauthenticated codeload returns 404 — the API surfaces that
as "not found or private". When ``GITHUB_TOKEN`` is configured it is sent as
a Bearer token (best-effort private access; fine-grained token UX is a
follow-up).
"""

from skillpack_import.github import (
    Fetcher,
    FetchError,
    FetchResponse,
    GithubTarget,
    HttpxFetcher,
    RepoSnapshot,
    SkillCandidate,
    extract_skill_files,
    fetch_repo,
    parse_github_url,
    scan_skills,
)
from skillpack_import.store import (
    ImportedPackResult,
    ImportedSkillResult,
    PackStoreError,
    PackSummary,
    SkillPayload,
    delete_pack,
    imported_root,
    list_packs,
    sanitize_dir_name,
    write_pack,
)

__all__ = [
    "FetchError",
    "FetchResponse",
    "Fetcher",
    "GithubTarget",
    "HttpxFetcher",
    "ImportedPackResult",
    "ImportedSkillResult",
    "PackStoreError",
    "PackSummary",
    "RepoSnapshot",
    "SkillCandidate",
    "SkillPayload",
    "delete_pack",
    "extract_skill_files",
    "fetch_repo",
    "imported_root",
    "list_packs",
    "parse_github_url",
    "sanitize_dir_name",
    "scan_skills",
    "write_pack",
]
