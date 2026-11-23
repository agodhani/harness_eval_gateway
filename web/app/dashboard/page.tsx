import { Suspense } from "react";
import DashboardClient from "./DashboardClient";
import { Loading } from "@/components/ui";

/**
 * /dashboard — server wrapper. The client streams in behind Suspense
 * under cacheComponents, same convention as /leaderboard.
 */
export default function Page() {
  return (
    <Suspense
      fallback={
        <main className="container">
          <Loading label="Loading dashboard…" />
        </main>
      }
    >
      <DashboardClient />
    </Suspense>
  );
}
