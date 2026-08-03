import { NavLink } from "react-router-dom";
import { cn } from "@/lib/utils";
import { NAV_ITEMS, type NavItem } from "@/config/nav";

// Segmented pill navigation. Each item is a route so sections stay deep-linkable.
//
// Only Matched jobs and Application history appear here. The bar shrink-wraps its two
// pills (w-fit) instead of stretching the full content width, so removing the other
// sections leaves a compact segmented control rather than a mostly-empty strip. Pill
// styling, border, padding and shadow are unchanged.
const pillClass = (isActive: boolean) =>
  cn(
    "rounded-[9px] border border-transparent px-3.5 py-1.5 text-[.86rem] font-semibold transition-all duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold focus-visible:ring-offset-1",
    isActive
      ? "bg-gradient-to-br from-[#F5A623] to-gold-deep text-white shadow-[0_2px_8px_rgba(217,119,6,.3)]"
      : "text-[#57534E] hover:bg-parchment-amber",
  );

function NavPill({ item }: { item: NavItem }) {
  return (
    <NavLink to={item.path} className={({ isActive }) => pillClass(isActive)}>
      {item.label}
    </NavLink>
  );
}

export function MainNav() {
  return (
    <nav
      aria-label="Dashboard sections"
      className="flex w-fit flex-wrap items-center gap-1.5 rounded-xl border border-parchment-border bg-card p-1.5 shadow-card"
    >
      {NAV_ITEMS.map((item) => (
        <NavPill key={item.path} item={item} />
      ))}
    </nav>
  );
}
