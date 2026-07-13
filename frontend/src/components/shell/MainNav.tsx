import { useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { ChevronDown } from "lucide-react";
import { cn } from "@/lib/utils";
import { ADVANCED_NAV_ITEMS, PRIMARY_NAV_ITEMS, type NavItem } from "@/config/nav";

// Segmented pill navigation, mirroring the Streamlit main_nav radio. Each item is a
// route so sections are deep-linkable (a frontend-only improvement over the radio).
//
// The three technical sections (AI Reasoning, AI Form Learning, Generalized Form Engine)
// sit inside a collapsed "Advanced" group so the primary bar stays recruiter-facing. Their
// routes are untouched and remain directly addressable by URL.
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
  const { pathname } = useLocation();
  // Collapsed by default. It starts open only when the current route *is* one of the
  // advanced sections, so a direct URL never hides the active item.
  const onAdvancedRoute = ADVANCED_NAV_ITEMS.some((item) => item.path === pathname);
  const [advancedOpen, setAdvancedOpen] = useState(onAdvancedRoute);

  return (
    <nav
      aria-label="Dashboard sections"
      className="rounded-xl border border-parchment-border bg-card p-1.5 shadow-card"
    >
      <div className="flex flex-wrap items-center gap-1.5">
        {PRIMARY_NAV_ITEMS.map((item) => (
          <NavPill key={item.path} item={item} />
        ))}

        <button
          type="button"
          onClick={() => setAdvancedOpen((open) => !open)}
          aria-expanded={advancedOpen}
          aria-controls="advanced-nav-group"
          className={cn(
            "ml-auto flex items-center gap-1.5 rounded-[9px] border border-dashed border-parchment-border px-3 py-1.5 text-[.8rem] font-medium text-[#8A8580] transition-all duration-200",
            "hover:bg-parchment-amber hover:text-[#57534E] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold focus-visible:ring-offset-1",
            advancedOpen && "bg-parchment-amber text-[#57534E]",
          )}
        >
          Advanced
          <ChevronDown
            aria-hidden="true"
            className={cn("size-3.5 transition-transform duration-200", advancedOpen && "rotate-180")}
          />
        </button>
      </div>

      {advancedOpen && (
        <div
          id="advanced-nav-group"
          className="mt-1.5 flex flex-wrap gap-1.5 border-t border-parchment-border pt-1.5"
        >
          {ADVANCED_NAV_ITEMS.map((item) => (
            <NavPill key={item.path} item={item} />
          ))}
        </div>
      )}
    </nav>
  );
}
