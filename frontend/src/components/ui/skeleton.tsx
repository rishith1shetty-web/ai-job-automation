import { cn } from "@/lib/utils";

// Shared loading placeholder. Replaces the ad-hoc `animate-pulse` blocks that were
// duplicated across pages so every loading state shimmers identically (and collapses to
// a static block under prefers-reduced-motion, handled globally in index.css).
export function Skeleton({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      role="status"
      aria-busy="true"
      aria-live="polite"
      className={cn("skeleton rounded-lg border border-parchment-border", className)}
      {...props}
    >
      <span className="sr-only">Loading…</span>
    </div>
  );
}
