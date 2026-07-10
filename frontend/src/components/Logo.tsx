import { useId } from "react";

// Rising Path Recruiters logo mark (sun + rising path arrow), ported verbatim from
// app/services/dashboard_theme.py. useId() gives unique gradient ids so multiple
// instances can coexist on one page without id collisions.
export function Logo({ size = 40, className }: { size?: number; className?: string }) {
  const uid = useId().replace(/:/g, "");
  const sun = `rpsun${uid}`;
  const arw = `rparw${uid}`;
  return (
    <svg
      viewBox="0 0 240 200"
      width={size}
      height={Math.round((size * 200) / 240)}
      role="img"
      aria-label="Rising Path Recruiters"
      className={className}
      style={{ display: "block" }}
    >
      <defs>
        <linearGradient id={sun} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#FFC93C" />
          <stop offset="1" stopColor="#F59E0B" />
        </linearGradient>
        <linearGradient id={arw} x1="0" y1="1" x2="1" y2="0">
          <stop offset="0" stopColor="#F5A623" />
          <stop offset="1" stopColor="#FFC93C" />
        </linearGradient>
      </defs>
      <g fill="#F5A623">
        <path d="M120 8 L124 34 L116 34 Z" />
        <path d="M158 16 L152 40 L145 37 Z" />
        <path d="M190 38 L174 56 L169 50 Z" />
        <path d="M212 72 L189 82 L187 75 Z" />
        <path d="M82 16 L88 40 L95 37 Z" />
        <path d="M50 38 L66 56 L71 50 Z" />
        <path d="M28 72 L51 82 L53 75 Z" />
        <path d="M20 108 L46 106 L46 113 Z" />
        <path d="M220 108 L194 106 L194 113 Z" />
      </g>
      <circle cx="120" cy="112" r="52" fill={`url(#${sun})`} />
      <path d="M22 168 L128 118 L150 118 L44 170 Z" fill="#8A5A2B" />
      <path d="M62 172 L158 124 L176 124 L92 174 Z" fill="#6B4423" />
      <path
        d="M118 156 L188 118 L182 108 L214 100 L208 133 L200 124 L134 160 Z"
        fill={`url(#${arw})`}
      />
      <path d="M150 174 L196 146 L216 174 Z" fill="#8A5A2B" />
    </svg>
  );
}
