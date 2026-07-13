import { cn } from "@/lib/utils";

// Single source of truth for the Rising Path Recruiters brand logo. Every logo in the
// app renders through this component, so the image path lives in exactly one place.
//
// The source is the exact uploaded PNG in frontend/public (served verbatim at the site
// root by Vite — no recompression, cropping, or redraw). The filename contains a space,
// so it is URL-encoded here. The intrinsic image is square, so width/height stay equal
// and the aspect ratio is preserved at every size.
const LOGO_SRC = "/Rising%20Path%20Final-01.png";

export interface LogoProps {
  /** Rendered box size in pixels (the square PNG scales to fit). Defaults to 40. */
  size?: number;
  className?: string;
}

export function Logo({ size = 40, className }: LogoProps) {
  return (
    <img
      src={LOGO_SRC}
      alt="Rising Path Recruiters"
      width={size}
      height={size}
      // Brand mark is above the fold on every route (TopNav/Sidebar), so load it eagerly
      // at high priority; explicit width/height already reserve space (no layout shift).
      // Every usage points at the same URL, so the browser fetches it exactly once.
      loading="eager"
      fetchPriority="high"
      decoding="async"
      draggable={false}
      className={cn("select-none object-contain", className)}
      style={{ width: size, height: size }}
    />
  );
}
