# Rising Path Recruiters — React Frontend

Phase 2 scaffold of the React/Lovable frontend migration. This app is a **visual
replacement** for the Streamlit dashboard (`dashboard.py`). It does **not** contain
any backend, automation, apply, dry-run, LinkedIn, Dice, matching, or database logic —
it only calls the existing FastAPI service over HTTP.

## Prerequisites

- Node.js 20+ (installed: 24.x LTS)
- The FastAPI backend running on `http://localhost:8000` (start it with the project's
  `start_app.ps1` at the repo root — unchanged by this migration).

## Run (development)

```bash
cd frontend
npm install
npm run dev
```

Open http://127.0.0.1:5173. The hello shell pings `GET /api/health` through the dev
proxy to confirm the backend is reachable.

## How the backend connection works (no CORS)

The browser only ever calls **same-origin** `/api/*`. The Vite dev proxy in
[`vite.config.ts`](./vite.config.ts) forwards those requests to FastAPI and strips the
`/api` prefix (`/api/portal/active` → `http://localhost:8000/portal/active`). Because the
browser sees same-origin requests, **no CORS middleware is added to the backend** — this
was the explicit architectural decision for the migration.

The proxy timeout is set to 700s because several backend endpoints block for minutes
(e.g. `POST /dice/connect` waits up to ~11 min for manual login).

## Stack

React 18 · TypeScript (strict) · Vite · Tailwind CSS · shadcn/ui (configured, components
added in Phase 3) · TanStack Query · React Router (added in Phase 3) · Framer Motion.

## Scripts

| Command | Purpose |
| --- | --- |
| `npm run dev` | Vite dev server with the API proxy |
| `npm run build` | Type-check + production build to `dist/` |
| `npm run preview` | Serve the production build locally |
| `npm run typecheck` | `tsc` with no emit |

## Migration status

- **Phase 2 (this):** scaffold, dev proxy, brand tokens, logo, health-check hello shell.
- **Phase 3:** static pages/components against recorded fixtures (no live writes).
- **Phase 4:** wire endpoints one at a time (reads → safe writes → sessions → apply, dry-run last).
- **Phases 5–7:** parity comparison, run both dashboards, final switch.

The Streamlit dashboard remains the source of truth until Phase 7.
