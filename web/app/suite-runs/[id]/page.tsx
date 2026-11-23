import { Suspense } from "react";
import SuiteRunClient from "./SuiteRunClient";
import { Loading } from "@/components/ui";

/**
 * /suite-runs/[id] — server wrapper (same params-is-runtime convention as
 * /suites/[slug] under cacheComponents).
 */
export default function Page({ params }: { params: Promise<{ id: string }> }) {
  return (
    <Suspense
      fallback={
        <main className="container">
          <Loading label="Loading suite run…" />
        </main>
      }
    >
      <SuiteRunLoader params={params} />
    </Suspense>
  );
}

async function SuiteRunLoader({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <SuiteRunClient id={id} />;
}
