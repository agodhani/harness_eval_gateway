import { Suspense } from "react";
import LeaderboardClient from "./LeaderboardClient";
import { Loading } from "@/components/ui";

/**
 * /leaderboard — server wrapper. The client reads `?preset=&since=&until=
 * &min_trials=` via useSearchParams (URL data → streams behind Suspense
 * under cacheComponents, same convention as /presets/[slug]).
 */
export default function Page() {
  return (
    <Suspense
      fallback={
        <main className="container">
          <Loading label="Loading leaderboard…" />
        </main>
      }
    >
      <LeaderboardClient />
    </Suspense>
  );
}
