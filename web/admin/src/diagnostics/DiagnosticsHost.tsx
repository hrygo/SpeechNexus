import { useRef, useState } from "react";
import { CapabilityCard } from "@speechnexus/shared";
import {
  asState,
  capabilityCopy,
  capabilityTitles,
  designState,
  sharedDeploymentCopy,
  statusProblemCopy,
} from "./copy";
import {
  WorkbenchStatusError,
  loadWorkbenchStatus,
  type WorkbenchStatus,
} from "./workbench-status";

export function DiagnosticsHost({
  load = loadWorkbenchStatus,
}: {
  load?: (signal?: AbortSignal) => Promise<WorkbenchStatus>;
}) {
  const [status, setStatus] = useState<WorkbenchStatus | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const controller = useRef<AbortController | null>(null);

  // Opening the admin entry reads nothing: the deployment is queried only when asked.
  function read() {
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;
    setLoading(true);
    void load(next.signal)
      .then((value) => {
        if (next.signal.aborted) return;
        setStatus(value);
        setProblem(null);
      })
      .catch((error: unknown) => {
        if (next.signal.aborted) return;
        setStatus(null);
        setProblem(error instanceof WorkbenchStatusError ? error.code : "status_unreachable");
      })
      .finally(() => {
        if (!next.signal.aborted) setLoading(false);
      });
  }

  const profile = status?.minutes_profile ?? null;

  return (
    <section aria-label="部署诊断" aria-busy={loading}>
      <h2>部署诊断</h2>
      <p>
        本页只读部署自身在 <code>/v1/workbench/status</code>{" "}
        上报的事实，不探测引擎、不读取凭据，也不显示未读取到的运行数据。
      </p>
      <button type="button" disabled={loading} onClick={read}>
        {status === null && problem === null ? "读取部署状态" : "刷新部署状态"}
      </button>
      {problem !== null && <p role="alert">{statusProblemCopy(problem)}</p>}
      {status !== null && (
        <>
          <p>
            工作台：{status.workbench_enabled ? "已启用" : "未启用"}；内网文本模型：
            {status.llm_enabled ? "已配置" : "未配置（默认关闭）"}
            {profile !== null
              ? `；模型 ${profile.llm_model}；模板修订 ${profile.template_revision}`
              : ""}
          </p>
          <p>{sharedDeploymentCopy(status.shared_deployment_notice)}</p>
          <section className="card-grid" aria-label="部署能力三态">
            {status.capabilities.map((item, index) => {
              const state = asState(item.status);
              return (
                <CapabilityCard
                  key={item.name}
                  label={`${String(index + 1).padStart(2, "0")} / Capability`}
                  title={capabilityTitles[item.name] ?? item.name}
                  description={
                    state === null
                      ? `服务端上报了无法识别的状态「${item.status}」；本页按未知处理，不视为可用。`
                      : `${capabilityCopy[state]}（服务端状态：${item.status}）`
                  }
                  state={state === null ? "planned" : designState(state)}
                />
              );
            })}
          </section>
        </>
      )}
      <p>
        尚未接入：队列深度、资源预算、租约、outbox、计量与 GPU
        事实。这些数据没有可读来源之前，此处保持为空。
      </p>
    </section>
  );
}

export default DiagnosticsHost;
