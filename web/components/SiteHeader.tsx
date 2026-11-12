"use client";

/**
 * Top navigation — brand + primary routes + API liveness dot.
 * Client component: HeaderNav uses usePathname (URL data → needs the
 * Suspense boundary under cacheComponents on dynamic routes); ApiDot only
 * probes /health in an effect.
 */
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { checkHealth } from "@/lib/api";

const LINKS = [
  { href: "/submit", label: "New run" },
  { href: "/runs", label: "Runs" },
] as const;

function HeaderNav() {
  const pathname = usePathname();
  return (
    <nav className="site-header__nav">
      {LINKS.map((l) => (
        <Link
          key={l.href}
          href={l.href}
          className={`site-header__link${pathname.startsWith(l.href) ? " is-active" : ""}`}
        >
          {l.label}
        </Link>
      ))}
    </nav>
  );
}

function ApiDot() {
  const [apiUp, setApiUp] = useState<boolean | null>(null);

  useEffect(() => {
    let cancelled = false;
    const probe = () => {
      checkHealth()
        .then((ok) => {
          if (!cancelled) setApiUp(ok);
        })
        .catch(() => {
          if (!cancelled) setApiUp(false);
        });
    };
    probe();
    const t = setInterval(probe, 15_000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  return (
    <div
      className={`api-dot api-dot--${apiUp === null ? "unknown" : apiUp ? "up" : "down"}`}
      title={
        apiUp === null
          ? "checking API…"
          : apiUp
            ? "control API reachable"
            : "control API unreachable"
      }
    >
      <span className="api-dot__label">
        {apiUp === null ? "api …" : apiUp ? "api up" : "api down"}
      </span>
    </div>
  );
}

export default function SiteHeader() {
  return (
    <header className="site-header">
      <div className="site-header__inner">
        <Link href="/" className="site-header__brand">
          harness_eval_gateway
        </Link>
        {/* usePathname is URL data — streams in behind Suspense on dynamic
            routes (cacheComponents); static routes resolve it at build. */}
        <Suspense
          fallback={
            <nav className="site-header__nav">
              {LINKS.map((l) => (
                <span key={l.href} className="site-header__link">
                  {l.label}
                </span>
              ))}
            </nav>
          }
        >
          <HeaderNav />
        </Suspense>
        <ApiDot />
      </div>
    </header>
  );
}
