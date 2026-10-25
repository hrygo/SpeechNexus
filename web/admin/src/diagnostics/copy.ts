export type CapabilityName =
  "transcription" | "meeting_minutes" | "assistant" | "teleprompter" | "audiobook_export";

export type CapabilityState = "available" | "disabled" | "unavailable";

export const capabilityTitles: Record<CapabilityName, string> = {
  transcription: "转写",
  meeting_minutes: "会议纪要",
  assistant: "语音助手",
  teleprompter: "提词阅读",
  audiobook_export: "有声书导出",
};

/**
 * What the deployment's verdict means, in the deployment's own terms. The admin entry
 * reports it; it never upgrades a state into readiness.
 */
export const capabilityCopy: Record<CapabilityState, string> = {
  available: "本部署已配置该能力所需的依赖，会承接对应请求。",
  disabled: "本部署未启用该能力：服务端状态为未启用。",
  unavailable: "本部署需要该能力，但依赖未就绪：配置齐备前对应请求不可用。",
};

/**
 * The design system has three presentation states; the server has its own three. The
 * mapping only downgrades: a missing dependency reads as `contract`, not `ready`.
 */
export function designState(state: CapabilityState): "ready" | "contract" | "planned" {
  return state === "available" ? "ready" : state === "unavailable" ? "contract" : "planned";
}

export const problemCopy: Record<string, string> = {
  status_unreachable: "无法读取部署状态：请求未到达服务端，本页不显示任何能力结论。",
  status_unreadable: "部署状态无法解析：响应不是合法 JSON，本页不显示任何能力结论。",
  status_invalid: "部署状态不符合契约：本页拒绝据此推断能力。",
};

export function statusProblemCopy(code: string): string {
  return problemCopy[code] ?? `读取部署状态失败（${code}）：本页不显示任何能力结论。`;
}

/**
 * The contract carries a code, not prose: `shared_deployment_notice` is the const string
 * `shared_deployment` and the UI must localize it. An unexpected code is reported as such
 * rather than echoed to operators as English.
 */
export function sharedDeploymentCopy(code: string): string {
  return code === "shared_deployment"
    ? "本部署为共享部署：项目、稿件、音色与结果由所有工作台使用者共享，不构成个人隔离。"
    : `部署边界声明为「${code}」，本页无法本地化该声明。`;
}

const KNOWN_STATES: readonly CapabilityState[] = ["available", "disabled", "unavailable"];

/** An unrecognised state is reported as unknown; it is never rounded up to available. */
export function asState(value: string): CapabilityState | null {
  return (KNOWN_STATES as readonly string[]).includes(value) ? (value as CapabilityState) : null;
}
