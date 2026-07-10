// The nine dashboard sections, matching the Streamlit main_nav radio options exactly.
// Order and labels are contractual — they mirror dashboard.py's nav_section list.
export interface NavItem {
  label: string;
  path: string;
}

export const NAV_ITEMS: NavItem[] = [
  { label: "Matched jobs", path: "/matched-jobs" },
  { label: "Application history", path: "/applications" },
  { label: "Profile", path: "/profile" },
  { label: "Employment History", path: "/employment-history" },
  { label: "Screening Knowledge", path: "/screening" },
  { label: "AI Reasoning", path: "/ai-reasoning" },
  { label: "AI Form Learning", path: "/form-learning" },
  { label: "Generalized Form Engine", path: "/generalized-form" },
  { label: "Performance", path: "/performance" },
];
