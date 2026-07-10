import type { Config } from "tailwindcss";
import animate from "tailwindcss-animate";

// Brand tokens ported verbatim from app/services/dashboard_theme.py so the React
// UI matches the Streamlit dashboard's Rising Path Recruiters identity exactly.
// Values are also exposed as CSS variables in src/index.css for shadcn/ui.
const config: Config = {
  darkMode: ["class"],
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    container: {
      center: true,
      padding: "2rem",
      screens: { "2xl": "1440px" },
    },
    extend: {
      colors: {
        // Rising Path brand palette
        gold: {
          DEFAULT: "#F0A824", // sun / arrow
          deep: "#D97706", // gradient end, hover
          text: "#A16207", // WCAG-safe amber for text on white
        },
        brown: {
          DEFAULT: "#8A5A2B", // path
          deep: "#6B4423", // dark path stripe
        },
        ink: "#3E2E1E", // headings (warm brown-black)
        parchment: {
          bg: "#FAF9F7", // app background
          card: "#FFFFFF",
          border: "#E7E0D8",
          amber: "#FEF3E2",
          sidebar: "#FAF7F2",
        },
        // shadcn/ui semantic tokens (driven by CSS vars, see index.css)
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
        },
        secondary: {
          DEFAULT: "hsl(var(--secondary))",
          foreground: "hsl(var(--secondary-foreground))",
        },
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
        },
        accent: {
          DEFAULT: "hsl(var(--accent))",
          foreground: "hsl(var(--accent-foreground))",
        },
        destructive: {
          DEFAULT: "hsl(var(--destructive))",
          foreground: "hsl(var(--destructive-foreground))",
        },
        card: {
          DEFAULT: "hsl(var(--card))",
          foreground: "hsl(var(--card-foreground))",
        },
      },
      fontFamily: {
        sans: ["Plus Jakarta Sans", "Segoe UI", "system-ui", "sans-serif"],
      },
      borderRadius: {
        lg: "14px",
        md: "10px",
        sm: "8px",
      },
      transitionTimingFunction: {
        brand: "cubic-bezier(.22,.61,.36,1)",
      },
      boxShadow: {
        card: "0 1px 2px rgba(62,46,30,.05), 0 4px 14px rgba(62,46,30,.06)",
        lift: "0 2px 4px rgba(62,46,30,.06), 0 12px 28px rgba(62,46,30,.11)",
      },
      keyframes: {
        "fade-in": {
          from: { opacity: "0", transform: "translateY(5px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
      },
      animation: {
        "fade-in": "fade-in .35s cubic-bezier(.22,.61,.36,1)",
      },
    },
  },
  plugins: [animate],
};

export default config;
