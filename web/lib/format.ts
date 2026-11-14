/**
 * Display formatting helpers. All timestamp rendering is done in UTC by
 * string-slicing the ISO value — deterministic between server prerender and
 * client hydration (no `Date` TZ dependence, no hydration mismatch).
 */

/** `2026-11-11T21:57:48.357662Z` → `11-11 21:57:48` (UTC, minute precision). */
export function formatTs(iso: string | null | undefined): string {
  if (!iso) return "—";
  // ISO strings from the API are UTC; slice rather than parse+format so the
  // output is identical on server and client regardless of local timezone.
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})/.exec(iso);
  if (!m) return iso;
  return `${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]}`;
}

/** `...T21:57:48.357662Z` → `21:57:48.357` — clock time with ms for the
 * timeline where same-second ordering matters. */
export function formatTimeMs(iso: string | null | undefined): string {
  if (!iso) return "—";
  const m = /T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?/.exec(iso);
  if (!m) return iso;
  const frac = m[4] ? `.${m[4].slice(0, 3).padEnd(3, "0")}` : "";
  return `${m[1]}:${m[2]}:${m[3]}${frac}`;
}

/** Short id for tables/badges: first 8 chars of a UUID. */
export function shortId(id: string | null | undefined, len = 8): string {
  if (!id) return "—";
  return id.length <= len ? id : id.slice(0, len);
}

/** Clamp + ellipsize a string for one-line previews. */
export function truncate(text: string, max = 80): string {
  const clean = text.replace(/\s+/g, " ").trim();
  return clean.length <= max ? clean : `${clean.slice(0, max - 1)}…`;
}

export function formatBytes(n: unknown): string {
  if (typeof n !== "number" || !Number.isFinite(n)) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatDuration(ms: unknown): string {
  if (typeof ms !== "number" || !Number.isFinite(ms)) return "";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const m = Math.floor(ms / 60_000);
  const s = Math.round((ms % 60_000) / 1000);
  return `${m}m ${s}s`;
}

/** Deterministic thousands separator (no toLocaleString — hydration-safe). */
export function formatInt(n: number): string {
  return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/** $-formatting for small estimate values: 4dp under a cent, else 2dp. */
export function formatUsd(cost: number): string {
  if (!Number.isFinite(cost)) return "—";
  if (cost === 0) return "$0.00";
  if (cost < 0.01) return `$${cost.toFixed(4)}`;
  return `$${cost.toFixed(2)}`;
}
