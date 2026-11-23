import { Suspense } from "react";
import SuiteDetailClient from "./SuiteDetailClient";
import { Loading } from "@/components/ui";

/**
 * /suites/[slug] — server wrapper (params is a runtime API under
 * cacheComponents → resolves inside Suspense; the view is a client
 * component, same convention as /presets/[slug]).
 */
export default function Page({ params }: { params: Promise<{ slug: string }> }) {
  return (
    <Suspense
      fallback={
        <main className="container">
          <Loading label="Loading suite…" />
        </main>
      }
    >
      <SuiteDetailLoader params={params} />
    </Suspense>
  );
}

async function SuiteDetailLoader({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  return <SuiteDetailClient slug={slug} />;
}
