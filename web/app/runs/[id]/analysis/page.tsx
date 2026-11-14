import { Suspense } from "react";
import AnalysisClient from "./AnalysisClient";
import { Loading } from "@/components/ui";

/**
 * /runs/[id]/analysis — server wrapper (params is a runtime API under
 * cacheComponents → resolve inside Suspense, render client component).
 */
export default function Page({ params }: { params: Promise<{ id: string }> }) {
  return (
    <Suspense
      fallback={
        <main className="container">
          <Loading label="Loading analysis…" />
        </main>
      }
    >
      <AnalysisLoader params={params} />
    </Suspense>
  );
}

async function AnalysisLoader({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <AnalysisClient runId={id} />;
}
