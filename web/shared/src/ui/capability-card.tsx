import { STATE_LABELS, type CapabilityState } from "../tokens";

export type CapabilityCardProps = {
  label: string;
  title: string;
  description: string;
  state: CapabilityState;
};

export function CapabilityCard({ label, title, description, state }: CapabilityCardProps) {
  return (
    <article className="capability-card">
      <div className="card-heading">
        <span className="card-label">{label}</span>
        <span className="card-state" data-state={state}>
          {STATE_LABELS[state]}
        </span>
      </div>
      <h2>{title}</h2>
      <p>{description}</p>
    </article>
  );
}
