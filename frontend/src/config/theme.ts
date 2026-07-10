// Brand token constants mirrored from app/services/dashboard_theme.py.
// Kept as a typed object for use in inline styles / charts where Tailwind classes
// are inconvenient. The canonical source of truth remains the Python theme; if that
// changes, update here and in tailwind.config.ts / index.css.
export const brand = {
  gold: "#F0A824",
  goldDeep: "#D97706",
  goldText: "#A16207",
  brown: "#8A5A2B",
  brownDeep: "#6B4423",
  ink: "#3E2E1E",
  body: "#57534E",
  bg: "#FAF9F7",
  card: "#FFFFFF",
  border: "#E7E0D8",
  green: "#15803D",
  red: "#B91C1C",
  amberBg: "#FEF3E2",
  sidebar: "#FAF7F2",
} as const;

export type BrandColor = keyof typeof brand;
