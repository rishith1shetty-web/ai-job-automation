import type { ReactElement, ReactNode } from "react";
import { render } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { SessionProvider } from "@/store/session";

// Renders a page with the same providers main.tsx uses, but with a throwaway
// QueryClient (no retries, no caching) so each test is isolated and fast.
export function renderWithProviders(ui: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  });
  const Wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <SessionProvider>
        <MemoryRouter>{children}</MemoryRouter>
      </SessionProvider>
    </QueryClientProvider>
  );
  return { queryClient, ...render(ui, { wrapper: Wrapper }) };
}
