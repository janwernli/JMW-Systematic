// Thin typed HTTP client. All trading logic lives in the Python backend; the UI only renders results.
import type { components } from "./schema";

export type S = components["schemas"];
export type StrategyConfig = S["StrategyConfig"];

export class ApiError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`/api${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    });
  } catch {
    throw new ApiError(0, "network", "Cannot reach the backend at /api. Is `npm run dev` (or the backend) running?");
  }
  const text = await res.text();
  const body = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const msg = body?.message ?? body?.detail ?? res.statusText;
    throw new ApiError(res.status, body?.error ?? "http_error", typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return body as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) => request<T>(path, { method: "POST", body: JSON.stringify(body ?? {}) }),
  put: <T>(path: string, body?: unknown) => request<T>(path, { method: "PUT", body: JSON.stringify(body ?? {}) }),
};
