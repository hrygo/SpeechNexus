import type { AssistantAvailability } from "./assistant-session";

/**
 * The UI state mirrors the deployment's own capability verdict. The three design states
 * are a presentation mapping of the server's answer, never an upgrade of it:
 * available → ready, unavailable → contract (wired, dependency missing),
 * disabled or unknown → planned.
 */
export function designState(availability: AssistantAvailability): "ready" | "contract" | "planned" {
  return availability === "available"
    ? "ready"
    : availability === "unavailable"
      ? "contract"
      : "planned";
}

export const availabilityCopy: Record<AssistantAvailability, string> = {
  available: "本部署已配置文本模型与语音底座，可提交助手回合。",
  disabled: "本部署未启用语音助手：服务端状态为未启用，创建入口保持关闭。",
  unavailable: "本部署缺少语音助手依赖：需要批准的内网文本模型与已配置的语音底座。",
  unknown: "尚未读取部署能力。读取成功前不会提交任何助手请求。",
};

export const assistantProblems = {
  unavailable: "当前部署尚未启用工作台，或连接不可用。",
  unknown: "结果尚未确认。请核对连接后按原键重试，不要重新创建。",
  missing: "回合已不可读取。",
  rejected: "请求被拒绝。请核对输入后重试。",
  conflict: "同一请求键已用于不同内容。请使用新的请求键。",
} as const;
