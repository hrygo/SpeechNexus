import { useId } from "react";
import { STATE_LABELS, type CapabilityState } from "../tokens";

export type CapabilityCardProps = {
  label: string;
  title: string;
  description: string;
  state: CapabilityState;
};

export function CapabilityCard({ label, title, description, state }: CapabilityCardProps) {
  const headingId = useId();

  return (
    <article className="capability-card" aria-labelledby={headingId}>
      <div className="card-heading">
        <span className="card-label">{label}</span>
        <span className="card-state" data-state={state}>
          {STATE_LABELS[state]}
        </span>
      </div>
      <h2 id={headingId}>{title}</h2>
      <p>{description}</p>
    </article>
  );
}
