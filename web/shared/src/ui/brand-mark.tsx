import { useId } from "react";
import type { NexusTone } from "../tokens";

export interface BrandMarkProps {
  size?: number;
  tone?: NexusTone;
  className?: string;
  ariaLabel?: string;
  decorative?: boolean;
}

export function BrandMark({
  size = 36,
  tone = "workbench",
  className,
  ariaLabel = "SpeechNexus · 声枢",
  decorative = false,
}: BrandMarkProps) {
  const rawId = useId().replace(/:/g, "");
  const gradientId = `sn-brand-accent-${tone}-${rawId}`;
  const bgGradientId = `sn-brand-bg-${rawId}`;

  const accentStop1 =
    tone === "admin" ? "var(--nexus-color-emerald-600)" : "var(--nexus-color-cobalt-600)";
  const accentStop2 =
    tone === "admin" ? "var(--nexus-color-emerald-300)" : "var(--nexus-color-cobalt-400)";
  const satelliteColor =
    tone === "admin" ? "var(--nexus-color-emerald-300)" : "var(--nexus-color-cobalt-300)";

  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 36 36"
      width={size}
      height={size}
      fill="none"
      className={className}
      role={decorative ? undefined : "img"}
      aria-label={decorative ? undefined : ariaLabel}
      aria-hidden={decorative ? "true" : undefined}
    >
      <defs>
        <linearGradient
          id={gradientId}
          x1="5"
          y1="5"
          x2="31"
          y2="31"
          gradientUnits="userSpaceOnUse"
        >
          <stop offset="0%" stopColor={accentStop1} />
          <stop offset="100%" stopColor={accentStop2} />
        </linearGradient>
        <linearGradient
          id={bgGradientId}
          x1="0"
          y1="0"
          x2="36"
          y2="36"
          gradientUnits="userSpaceOnUse"
        >
          <stop offset="0%" stopColor="var(--nexus-color-obsidian-900)" />
          <stop offset="100%" stopColor="var(--nexus-color-obsidian-950)" />
        </linearGradient>
      </defs>

      {/* Base Squircle Container */}
      <rect width="36" height="36" rx="10" fill={`url(#${bgGradientId})`} />
      <rect
        x="0.5"
        y="0.5"
        width="35"
        height="35"
        rx="9.5"
        stroke="var(--specular-line)"
        strokeWidth="1"
      />

      {/* Acoustic Orbital Ring (声枢 Acoustic Hub) */}
      <circle
        cx="18"
        cy="18"
        r="10.5"
        stroke={`url(#${gradientId})`}
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeDasharray="48 18"
        transform="rotate(-35 18 18)"
      />

      {/* Symmetrical 5-Bar Soundwave Pulse */}
      <path
        d="M12 16.5V19.5M15 13.5V22.5M18 10.5V25.5M21 13.5V22.5M24 16.5V19.5"
        stroke="var(--nexus-color-obsidian-50)"
        strokeWidth="2.2"
        strokeLinecap="round"
      />

      {/* Nexus Satellite Node */}
      <circle cx="25.2" cy="10.8" r="2.2" fill={satelliteColor} />
    </svg>
  );
}
