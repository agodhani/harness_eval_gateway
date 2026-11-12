import { Suspense } from "react";
import ResultsClient from "./ResultsClient";
import { Loading } from "@/components/ui";

/**
 * /runs/[id]/results — server wrapper (params is a runtime API under
 * cacheComponents → resolve inside Suspense, render client component).
 */
export default function Page({ params }: { params: Promise<{ id: string }> }) {
  return (
    <Suspense
      fallback={
        <main className="container">
          <Loading label="Loading results…" />
        </main>
      }
    >
      <ResultsLoader params={params} />
    </Suspense>
  );
}

async function ResultsLoader({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <ResultsClient runId={id} />;
}
