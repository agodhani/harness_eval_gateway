import Hero from "@/components/landing/Hero";
import Method from "@/components/landing/Method";
import Pipeline from "@/components/landing/Pipeline";
import Gateway from "@/components/landing/Gateway";
import Adapters from "@/components/landing/Adapters";
import Telemetry from "@/components/landing/Telemetry";
import Results from "@/components/landing/Results";
import Quickstart from "@/components/landing/Quickstart";
import LandingFooter from "@/components/landing/LandingFooter";
import "./landing.css";

export default function Home() {
  return (
    <main className="lp lp-noise">
      <Hero />
      <Method />
      <Pipeline />
      <Gateway />
      <Adapters />
      <Telemetry />
      <Results />
      <Quickstart />
      <LandingFooter />
    </main>
  );
}
