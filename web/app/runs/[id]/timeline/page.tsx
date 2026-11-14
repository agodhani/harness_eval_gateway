import { Suspense } from "react";
import TimelineClient from "./TimelineClient";
import { Loading } from "@/components/ui";

/**
 * /runs/[id]/timeline — server wrapper (params is a runtime API under
 * cacheComponents → resolve inside Suspense, render client component).
 */
export default function Page({ params }: { params: Promise<{ id: string }> }) {
  return (
    <Suspense
      fallback={
        <main className="container">
          <Loading label="Loading timeline…" />
        </main>
      }
    >
      <TimelineLoader params={params} />
    </Suspense>
  );
}

async function TimelineLoader({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  // key={id} — the App Router reuses the client component instance navigating
  // /runs/A/timeline → /runs/B/timeline; a fresh key remounts it so no events,
  // cursor, filters, or split lanes leak across runs.
  return <TimelineClient key={id} runId={id} />;
}
