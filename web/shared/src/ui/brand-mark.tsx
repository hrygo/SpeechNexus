import type { NexusTone } from "../tokens";

export interface BrandMarkProps {
  size?: number;
  tone?: NexusTone;
  className?: string;
  ariaLabel?: string;
}

export function BrandMark({
  size = 36,
  tone = "workbench",
  className,
  ariaLabel = "SpeechNexus · 声枢",
}: BrandMarkProps) {
  const gradientId = `sn-brand-accent-${tone}`;
  const accentStop1 = tone === "admin" ? "#087B62" : "#1D4ED8";
  const accentStop2 = tone === "admin" ? "#5EEAD4" : "#60A5FA";
  const satelliteColor = tone === "admin" ? "#5EEAD4" : "#93C5FD";

  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 36 36"
      width={size}
      height={size}
      fill="none"
      className={className}
      role="img"
      aria-label={ariaLabel}
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
          id="sn-brand-bg"
          x1="0"
          y1="0"
          x2="36"
          y2="36"
          gradientUnits="userSpaceOnUse"
        >
          <stop offset="0%" stopColor="var(--ink, #18201C)" />
          <stop offset="100%" stopColor="color-mix(in srgb, var(--ink, #18201C) 80%, black)" />
        </linearGradient>
      </defs>

      {/* Base Squircle Container */}
      <rect width="36" height="36" rx="10" fill="url(#sn-brand-bg)" />
      <rect
        x="0.5"
        y="0.5"
        width="35"
        height="35"
        rx="9.5"
        stroke="var(--specular-line, rgba(255,255,255,0.14))"
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
        stroke="var(--surface-strong, #F8FAFC)"
        strokeWidth="2.2"
        strokeLinecap="round"
      />

      {/* Nexus Satellite Node */}
      <circle cx="25.2" cy="10.8" r="2.2" fill={satelliteColor} />
    </svg>
  );
}
