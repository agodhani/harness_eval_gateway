/**
 * Shared UI primitives — plain CSS classes defined in app/globals.css.
 * No UI framework; keep these tiny and composable.
 */
import type { ReactNode } from "react";

// ---------------------------------------------------------------------------
// Badge — colored status/source/type pill
// ---------------------------------------------------------------------------

type BadgeTone =
  | "gray"
  | "blue"
  | "green"
  | "red"
  | "amber"
  | "violet"
  | "teal"
  | "outline";

const TONE_CLASS: Record<BadgeTone, string> = {
  gray: "badge--gray",
  blue: "badge--blue",
  green: "badge--green",
  red: "badge--red",
  amber: "badge--amber",
  violet: "badge--violet",
  teal: "badge--teal",
  outline: "badge--outline",
};

export function Badge({
  tone = "gray",
  children,
  title,
}: {
  tone?: BadgeTone;
  children: ReactNode;
  title?: string;
}) {
  return (
    <span className={`badge ${TONE_CLASS[tone]}`} title={title}>
      {children}
    </span>
  );
}

/** Map a run/sandbox/rubric status string to a badge tone. */
export function statusTone(
  status: string,
): "gray" | "blue" | "green" | "red" | "amber" | "violet" | "teal" {
  switch (status) {
    case "done":
    case "approved":
    case "agent_done":
    case "harness_complete":
      return "green";
    case "failed":
    case "error":
    case "timeout":
    case "budget_exceeded":
      return "red";
    case "running":
      return "blue";
    case "evaluating":
      return "violet";
    case "provisioning":
    case "terminating":
      return "amber";
    case "submitted":
    case "rubric_review":
    case "stuck_loop":
      return "teal";
    default:
      return "gray";
  }
}

/** Map an event source to its badge tone (timeline color-coding). */
export function sourceTone(source: string): "blue" | "violet" | "teal" | "amber" | "gray" {
  switch (source) {
    case "gateway":
      return "blue";
    case "harness":
      return "violet";
    case "sandbox":
      return "teal";
    case "evaluator":
      return "amber";
    case "orchestrator":
      return "gray";
    default:
      return "gray";
  }
}

// ---------------------------------------------------------------------------
// Card — section container
// ---------------------------------------------------------------------------

export function Card({
  title,
  actions,
  children,
  className = "",
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`card ${className}`}>
      {(title || actions) && (
        <header className="card__header">
          {title ? <h2 className="card__title">{title}</h2> : <span />}
          {actions ? <div className="card__actions">{actions}</div> : null}
        </header>
      )}
      <div className="card__body">{children}</div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Feedback — loading / error / empty / info notices
// ---------------------------------------------------------------------------

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="notice notice--muted" role="status">
      <span className="spinner" aria-hidden />
      {label}
    </div>
  );
}

export function ErrorNote({
  title = "Something went wrong",
  children,
}: {
  title?: string;
  children?: ReactNode;
}) {
  return (
    <div className="notice notice--error" role="alert">
      <strong>{title}</strong>
      {children ? <div className="notice__detail">{children}</div> : null}
    </div>
  );
}

export function InfoNote({ children }: { children: ReactNode }) {
  return <div className="notice notice--info">{children}</div>;
}

export function WarnNote({ children }: { children: ReactNode }) {
  return <div className="notice notice--warn">{children}</div>;
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <div className="empty-state">{children}</div>;
}
