import type { ReactNode } from "react";
import Reveal from "./Reveal";

type SectionProps = {
  id: string;
  index: string;
  kicker: string;
  title: ReactNode;
  lede?: ReactNode;
  children: ReactNode;
};

export default function Section({
  id,
  index,
  kicker,
  title,
  lede,
  children,
}: SectionProps) {
  return (
    <section id={id} className="lp-section">
      <div className="lp-wrap">
        <header className="lp-section__head">
          <Reveal>
            <div className="lp-section__meta">
              <span className="lp-section__index">{index}</span>
              <span className="kicker">{kicker}</span>
            </div>
            <div className="lp-section__rule" />
          </Reveal>
          <div>
            <Reveal delay={80}>
              <h2 className="lp-section__title">{title}</h2>
            </Reveal>
            {lede ? (
              <Reveal delay={160}>
                <p className="lp-section__lede">{lede}</p>
              </Reveal>
            ) : null}
          </div>
        </header>
        <div className="lp-section__body">{children}</div>
      </div>
    </section>
  );
}
