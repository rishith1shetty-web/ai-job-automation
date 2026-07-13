// The nine dashboard sections. Labels and routes are contractual — they mirror
// dashboard.py's nav_section list and must not be renamed.
//
// The navigation is split into two groups purely for presentation: the six recruiter-facing
// sections are primary, and the three technical/evidence sections live behind a collapsed
// "Advanced" group. Every section keeps its route, page, hooks and behaviour — the split
// only changes what the recruiter sees first.
export interface NavItem {
  label: string;
  path: string;
}

/** Recruiter-facing sections, shown as the primary navigation. */
export const PRIMARY_NAV_ITEMS: NavItem[] = [
  { label: "Matched jobs", path: "/matched-jobs" },
  { label: "Application history", path: "/applications" },
  { label: "Profile", path: "/profile" },
  { label: "Employment History", path: "/employment-history" },
  { label: "Screening Knowledge", path: "/screening" },
  { label: "Performance", path: "/performance" },
];

/** Technical evidence sections, reachable via the Advanced group and by direct URL. */
export const ADVANCED_NAV_ITEMS: NavItem[] = [
  { label: "AI Reasoning", path: "/ai-reasoning" },
  { label: "AI Form Learning", path: "/form-learning" },
  { label: "Generalized Form Engine", path: "/generalized-form" },
];
