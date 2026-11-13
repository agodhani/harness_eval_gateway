import { Suspense } from "react";
import RunDetailClient from "./RunDetailClient";
import { Loading } from "@/components/ui";

/**
 * /runs/[id] — server wrapper. `params` is a runtime API under
 * cacheComponents, so the dynamic segment resolves inside a Suspense
 * boundary and the interactive detail view is a client component.
 */
export default function Page({ params }: { params: Promise<{ id: string }> }) {
  return (
    <Suspense
      fallback={
        <main className="container">
          <Loading label="Loading run…" />
        </main>
      }
    >
      <RunDetailLoader params={params} />
    </Suspense>
  );
}

async function RunDetailLoader({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <RunDetailClient runId={id} />;
}
