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
  accentDarkVar: string;
  description: string;
}

export const TONE_CONFIG: Record<NexusTone, ToneConfig> = {
  admin: {
    tone: "admin",
    name: "AdminUI",
    role: "运行事实管理",
    accentVar: "var(--nexus-color-emerald-500)",
    accentDarkVar: "var(--nexus-color-emerald-400)",
    description: "Precision Fleet Cockpit — 模型绑定、资源预算、任务维护与全链路诊断",
  },
  workbench: {
    tone: "workbench",
    name: "个人语音工作台",
    role: "语音任务集中完成",
    accentVar: "var(--nexus-color-cobalt-500)",
    accentDarkVar: "var(--nexus-color-cobalt-300)",
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
    obsidian800: "var(--nexus-color-obsidian-800)",
    obsidian700: "var(--nexus-color-obsidian-700)",
    obsidian500: "var(--nexus-color-obsidian-500)",
    obsidian300: "var(--nexus-color-obsidian-300)",
    obsidian100: "var(--nexus-color-obsidian-100)",
    obsidian50: "var(--nexus-color-obsidian-50)",
    sand50: "var(--nexus-color-sand-50)",
    sand100: "var(--nexus-color-sand-100)",
    sand200: "var(--nexus-color-sand-200)",
    sand300: "var(--nexus-color-sand-300)",
    sand700: "var(--nexus-color-sand-700)",
    sand900: "var(--nexus-color-sand-900)",
    emerald300: "var(--nexus-color-emerald-300)",
    emerald400: "var(--nexus-color-emerald-400)",
    emerald500: "var(--nexus-color-emerald-500)",
    emerald600: "var(--nexus-color-emerald-600)",
    emerald900: "var(--nexus-color-emerald-900)",
    cobalt300: "var(--nexus-color-cobalt-300)",
    cobalt400: "var(--nexus-color-cobalt-400)",
    cobalt500: "var(--nexus-color-cobalt-500)",
    cobalt600: "var(--nexus-color-cobalt-600)",
    cobalt900: "var(--nexus-color-cobalt-900)",
    amber400: "var(--nexus-color-amber-400)",
    amber500: "var(--nexus-color-amber-500)",
    rose500: "var(--nexus-color-rose-500)",
    violet500: "var(--nexus-color-violet-500)",
    coral500: "var(--nexus-color-coral-500)",
  },
  semantics: {
    page: "var(--bg-page)",
    surface: "var(--bg-surface)",
    surfaceRaised: "var(--bg-surface-raised)",
    surfaceOverlay: "var(--bg-surface-overlay)",
    surfaceGlass: "var(--bg-surface-glass)",
    ink: "var(--text-primary)",
    muted: "var(--text-secondary)",
    mutedSubtle: "var(--text-muted)",
    textInverse: "var(--text-inverse)",
    line: "var(--border-subtle)",
    lineStrong: "var(--border-strong)",
    specularLine: "var(--border-highlight)",
    accent: "var(--accent-primary)",
    warning: "var(--warning)",
    waveformWave: "var(--waveform-wave)",
    waveformProgress: "var(--waveform-progress)",
    waveformCursor: "var(--waveform-cursor)",
    speaker1: "var(--speaker-1)",
    speaker2: "var(--speaker-2)",
    speaker3: "var(--speaker-3)",
    speaker4: "var(--speaker-4)",
    metricHealthy: "var(--metric-healthy)",
    metricBusy: "var(--metric-busy)",
    metricDanger: "var(--metric-danger)",
  },
  typography: {
    fontSans: "var(--nexus-font-sans)",
    fontMono: "var(--nexus-font-mono)",
    xs: "var(--nexus-text-xs)",
    sm: "var(--nexus-text-sm)",
    base: "var(--nexus-text-base)",
    md: "var(--nexus-text-md)",
    lg: "var(--nexus-text-lg)",
    xl: "var(--nexus-text-xl)",
    xl2: "var(--nexus-text-2xl)",
    hero: "var(--nexus-text-hero)",
  },
  space: {
    s1: "var(--nexus-space-1)",
    s2: "var(--nexus-space-2)",
    s3: "var(--nexus-space-3)",
    s4: "var(--nexus-space-4)",
    s5: "var(--nexus-space-5)",
    s6: "var(--nexus-space-6)",
    s8: "var(--nexus-space-8)",
    s12: "var(--nexus-space-12)",
    s16: "var(--nexus-space-16)",
  },
  radii: {
    xs: "var(--nexus-radius-xs)",
    sm: "var(--nexus-radius-sm)",
    md: "var(--nexus-radius-md)",
    lg: "var(--nexus-radius-lg)",
    xl: "var(--nexus-radius-xl)",
    medium: "var(--radius-medium)",
    large: "var(--radius-large)",
    full: "var(--nexus-radius-full)",
  },
} as const;
