import { NavLink } from "react-router-dom";
import { cn } from "@/lib/utils";
import { NAV_ITEMS } from "@/config/nav";

// Segmented pill navigation, mirroring the Streamlit main_nav radio. Each item is a
// route so sections are deep-linkable (a frontend-only improvement over the radio).
export function MainNav() {
  return (
    <nav className="flex flex-wrap gap-1.5 rounded-xl border border-parchment-border bg-card p-1.5 shadow-card">
      {NAV_ITEMS.map((item) => (
        <NavLink
          key={item.path}
          to={item.path}
          className={({ isActive }) =>
            cn(
              "rounded-[9px] border border-transparent px-3.5 py-1.5 text-[.86rem] font-semibold transition-all duration-200",
              isActive
                ? "bg-gradient-to-br from-[#F5A623] to-gold-deep text-white shadow-[0_2px_8px_rgba(217,119,6,.3)]"
                : "text-[#57534E] hover:bg-parchment-amber",
            )
          }
        >
          {item.label}
        </NavLink>
      ))}
    </nav>
  );
}
