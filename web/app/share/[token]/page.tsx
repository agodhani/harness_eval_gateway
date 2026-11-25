import { Suspense } from "react";
import ShareClient from "./ShareClient";
import { Loading } from "@/components/ui";

/**
 * /share/[token] — the read-only share view (PR35). Server wrapper: params
 * is a runtime API under cacheComponents → resolve inside Suspense, render
 * the client component.
 */
export default function Page({ params }: { params: Promise<{ token: string }> }) {
  return (
    <Suspense
      fallback={
        <main className="container">
          <Loading label="Loading shared run…" />
        </main>
      }
    >
      <ShareLoader params={params} />
    </Suspense>
  );
}

async function ShareLoader({ params }: { params: Promise<{ token: string }> }) {
  const { token } = await params;
  return <ShareClient token={token} />;
}
