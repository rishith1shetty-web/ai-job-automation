// Thin fetch wrapper. Every call goes to the same-origin "/api" prefix, which the
// Vite dev proxy forwards to the FastAPI service (see vite.config.ts). In a future
// production build the same "/api" prefix can be served same-origin, so no code here
// changes between dev and prod.

export const API_PREFIX = "/api";

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

async function handleResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let detail: unknown = null;
    try {
      detail = (await response.json())?.detail ?? null;
    } catch {
      detail = await response.text().catch(() => null);
    }
    throw new ApiError(
      response.status,
      detail,
      typeof detail === "string" ? detail : `Request failed (${response.status})`,
    );
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export async function apiRequest<T = unknown>(
  method: string,
  path: string,
  options: { body?: unknown; signal?: AbortSignal } = {},
): Promise<T> {
  const init: RequestInit = { method, signal: options.signal, headers: {} };
  if (options.body !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(options.body);
  }
  return handleResponse<T>(await fetch(`${API_PREFIX}${path}`, init));
}

/** Multipart POST (e.g. resume upload). The browser sets the multipart boundary; do
 *  not set Content-Type manually. */
export async function apiUpload<T = unknown>(
  path: string,
  formData: FormData,
  signal?: AbortSignal,
): Promise<T> {
  return handleResponse<T>(await fetch(`${API_PREFIX}${path}`, { method: "POST", body: formData, signal }));
}
