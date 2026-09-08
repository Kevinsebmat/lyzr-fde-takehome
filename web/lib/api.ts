/** Thin fetch wrapper. Next rewrites /api/* to the FastAPI app. */

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    // Surface FastAPI's own message where it has one — a validation error that
    // says which field is wrong is more use than "request failed".
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* non-JSON error body; the status line is all we have */
    }
    throw new ApiError(detail, res.status);
  }
  return res.json() as Promise<T>;
}

export const get = <T,>(path: string) => request<T>(path);

export const post = <T,>(path: string, body?: unknown, headers?: Record<string, string>) =>
  request<T>(path, {
    method: "POST",
    body: body === undefined ? undefined : JSON.stringify(body),
    headers,
  });

export interface Health {
  ok: boolean;
  mode: "mock" | "live";
  live: boolean;
  note: string;
}

export interface Project {
  slug: string;
  name: string;
  failure_mode: string;
}
