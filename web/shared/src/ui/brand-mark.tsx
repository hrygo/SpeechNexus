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
  ariaLabel = "SpeechNexus",
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
          x1="4"
          y1="4"
          x2="32"
          y2="32"
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
        width="36"
        height="36"
        rx="10"
        stroke="var(--specular-line, rgba(255,255,255,0.12))"
        strokeWidth="1"
      />

      {/* Acoustic Orbital Ring (Nexus Core) */}
      <circle
        cx="18"
        cy="18"
        r="9"
        stroke={`url(#${gradientId})`}
        strokeWidth="2"
        strokeLinecap="round"
        strokeDasharray="28 12"
      />

      {/* Soundwave Pulse Bars */}
      <path
        d="M12 18V18.01M15 15V21M18 12V24M21 15V21M24 18V18.01"
        stroke="var(--surface-strong, #FFFFFF)"
        strokeWidth="2.2"
        strokeLinecap="round"
      />

      {/* Nexus Satellite Node */}
      <circle cx="24.5" cy="11.5" r="2" fill={satelliteColor} />
    </svg>
  );
}
