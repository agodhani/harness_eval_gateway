import { Suspense } from "react";
import PresetDetailClient from "./PresetDetailClient";
import { Loading } from "@/components/ui";

/**
 * /presets/[slug] — server wrapper (params is a runtime API under
 * cacheComponents → resolves inside Suspense; the view is a client
 * component, same convention as /runs/[id]).
 */
export default function Page({ params }: { params: Promise<{ slug: string }> }) {
  return (
    <Suspense
      fallback={
        <main className="container">
          <Loading label="Loading preset…" />
        </main>
      }
    >
      <PresetDetailLoader params={params} />
    </Suspense>
  );
}

async function PresetDetailLoader({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  return <PresetDetailClient slug={slug} />;
}
