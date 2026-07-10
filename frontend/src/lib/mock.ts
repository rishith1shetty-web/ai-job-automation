// Phase 3 mock action layer. Buttons are visually present and clickable, but they
// perform NO backend calls — they only log to the console and (optionally) flash an
// inline notice. Phase 4 replaces these with real TanStack Query mutations.
export function mockAction(label: string): void {
  // eslint-disable-next-line no-console
  console.info(`[mock] "${label}" clicked — inert in Phase 3 (no API call).`);
}
