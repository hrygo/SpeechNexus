/**
 * SpeechNexus Typed Design Tokens & Contracts
 */

export type NexusTone = "admin" | "workbench";

export type CapabilityState = "ready" | "contract" | "planned";

export interface BrandIdentity {
  name: string;
  cnName: string;
  archetype: string;
  sloganCn: string;
  sloganEn: string;
}

export const BRAND_IDENTITY: BrandIdentity = {
  name: "SpeechNexus",
  cnName: "声枢",
  archetype: "Acoustic Hub",
  sloganCn: "自部署的语音服务与创作中枢",
  sloganEn: "Self-Hosted Speech Engine & Studio",
} as const;

export interface ToneConfig {
  tone: NexusTone;
  name: string;
  role: string;
  accentVar: string;
  accentHexLight: string;
  accentHexDark: string;
  description: string;
}

export const TONE_CONFIG: Record<NexusTone, ToneConfig> = {
  admin: {
    tone: "admin",
    name: "AdminUI",
    role: "运行事实管理",
    accentVar: "--nexus-color-emerald-500",
    accentHexLight: "#087B62",
    accentHexDark: "#5EEAD4",
    description: "Precision Fleet Cockpit — 模型绑定、资源预算、任务维护与全链路诊断",
  },
  workbench: {
    tone: "workbench",
    name: "个人语音工作台",
    role: "语音任务集中完成",
    accentVar: "--nexus-color-cobalt-500",
    accentHexLight: "#1D4ED8",
    accentHexDark: "#60A5FA",
    description: "Next-Gen Audio Atelier — 录音转写、长文配音、实时会议、同传字幕与有声书制作",
  },
} as const;

export const STATE_LABELS: Record<CapabilityState, string> = {
  ready: "基础就绪",
  contract: "契约已交付",
  planned: "后续阶段",
} as const;

export const NEXUS_TOKENS = {
  colors: {
    obsidian950: "var(--nexus-color-obsidian-950)",
    obsidian900: "var(--nexus-color-obsidian-900)",
    obsidian850: "var(--nexus-color-obsidian-850)",
    obsidian50: "var(--nexus-color-obsidian-50)",
    sand50: "var(--nexus-color-sand-50)",
    sand900: "var(--nexus-color-sand-900)",
    emerald500: "var(--nexus-color-emerald-500)",
    emerald400: "var(--nexus-color-emerald-400)",
    cobalt500: "var(--nexus-color-cobalt-500)",
    cobalt300: "var(--nexus-color-cobalt-300)",
  },
  semantics: {
    page: "var(--page)",
    surface: "var(--surface)",
    surfaceGlass: "var(--surface-glass)",
    ink: "var(--ink)",
    muted: "var(--muted)",
    line: "var(--line)",
    accent: "var(--accent)",
    warning: "var(--warning)",
    waveformWave: "var(--waveform-wave)",
    waveformProgress: "var(--waveform-progress)",
  },
  radii: {
    sm: "var(--radius-sm)",
    md: "var(--radius-md)",
    medium: "var(--radius-medium)",
    large: "var(--radius-large)",
    full: "var(--radius-full)",
  },
} as const;
