import { ApiError } from "./api";

// Frontend port of dashboard.py's api_error_message(): produce a clean, user-facing
// message from a failed request. Browser fetch throws TypeError on network failure
// (the analogue of requests.ConnectionError); FastAPI HTTPExceptions arrive as an
// ApiError whose `detail` is the backend message.
export function apiErrorMessage(action: string, err: unknown): string {
  if (err instanceof ApiError) {
    if (typeof err.detail === "string" && err.detail) return `${action} failed: ${err.detail}`;
    return `${action} failed: ${err.message}`;
  }
  if (err instanceof DOMException && err.name === "AbortError") {
    return `${action} timed out. The backend may still be working; refresh in a moment.`;
  }
  if (err instanceof TypeError) {
    return `${action} could not reach FastAPI. Check that the backend is running.`;
  }
  return `${action} failed: ${String(err)}`;
}
