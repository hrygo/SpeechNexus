import type { ReactNode } from "react";
import { BrandMark } from "./brand-mark";
import type { NexusTone } from "../tokens";

import "./styles.css";

export type AppShellProps = {
  surface: "AdminUI" | "个人语音工作台";
  tone: NexusTone;
  eyebrow: string;
  title: string;
  description: string;
  boundaryNote: string;
  children: ReactNode;
};

export function AppShell({
  surface,
  tone,
  eyebrow,
  title,
  description,
  boundaryNote,
  children,
}: AppShellProps) {
  return (
    <div className="app-shell" data-tone={tone}>
      <a className="skip-link" href="#main-content">
        跳到主要内容
      </a>
      <header className="site-header">
        <a className="brand" href="/" aria-label="SpeechNexus 首页">
          <BrandMark tone={tone} size={36} />
          <span>SpeechNexus</span>
        </a>
        <div className="header-meta">
          <span className="airgap-badge" title="模型本地离线推理，数据不出网">
            本地离线 · 音频不出网
          </span>
          <span className="surface-label">{surface}</span>
        </div>
      </header>

      <main id="main-content">
        <section className="hero" aria-labelledby="page-title">
          <div className="hero-copy">
            <p className="eyebrow">{eyebrow}</p>
            <h1 id="page-title">{title}</h1>
            <p className="hero-description">{description}</p>
          </div>
          <div className="hero-status" role="status">
            <span className="status-dot" aria-hidden="true" />
            <span>
              <strong>工程外壳已就绪</strong>
              <small>契约已交付，业务实现待接入</small>
            </span>
          </div>
        </section>

        <aside className="boundary-note" aria-label="部署数据边界">
          <span className="boundary-label">共享部署</span>
          <p>{boundaryNote}</p>
        </aside>

        {children}
      </main>

      <footer>
        <span>SpeechNexus engineering baseline</span>
        <span>无登录 · 可信共享网络</span>
      </footer>
    </div>
  );
}
