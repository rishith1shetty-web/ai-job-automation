// The dashboard navigation. Only the two recruiter-facing sections are reachable from
// the navigation bar: Matched jobs and Application history.
//
// The other sections (Profile, Employment History, Screening Knowledge, Performance,
// AI Reasoning, AI Form Learning, Generalized Form Engine) keep their routes, pages,
// hooks and backend endpoints — they are simply no longer surfaced here, and are not
// hidden behind another menu either. App.tsx still resolves them by direct URL.
export interface NavItem {
  label: string;
  path: string;
}

export const NAV_ITEMS: NavItem[] = [
  { label: "Matched jobs", path: "/matched-jobs" },
  { label: "Application history", path: "/applications" },
];
