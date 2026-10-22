export type CapabilityCardProps = {
  label: string;
  title: string;
  description: string;
  state: "ready" | "contract" | "planned";
};

const stateLabels = {
  ready: "基础就绪",
  contract: "等待 P0 契约",
  planned: "后续阶段",
} as const;

export function CapabilityCard({ label, title, description, state }: CapabilityCardProps) {
  return (
    <article className="capability-card">
      <div className="card-heading">
        <span className="card-label">{label}</span>
        <span className="card-state" data-state={state}>
          {stateLabels[state]}
        </span>
      </div>
      <h2>{title}</h2>
      <p>{description}</p>
    </article>
  );
}
