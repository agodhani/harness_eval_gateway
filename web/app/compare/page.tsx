import { Suspense } from "react";
import CompareClient from "./CompareClient";
import { Loading } from "@/components/ui";

/**
 * /compare — server wrapper. The client reads `?runs=id1,id2` via
 * useSearchParams (URL data → streams behind Suspense under
 * cacheComponents, same convention as /presets/[slug]).
 */
export default function Page() {
  return (
    <Suspense
      fallback={
        <main className="container">
          <Loading label="Loading compare…" />
        </main>
      }
    >
      <CompareClient />
    </Suspense>
  );
}
