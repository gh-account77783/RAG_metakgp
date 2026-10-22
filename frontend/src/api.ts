export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  constructor(code: string, message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

function csrf(): string {
  const value = document.cookie.split("; ").find(item => item.startsWith("graphmind_csrf="));
  return decodeURIComponent(value?.slice("graphmind_csrf=".length) ?? "");
}

export class ApiClient {
  private renewal: Promise<void> | null = null;
  private readonly fetcher: typeof fetch;
  private readonly token: () => string;
  constructor(fetcher: typeof fetch = globalThis.fetch.bind(globalThis), token: () => string = csrf) {
    this.fetcher = fetcher;
    this.token = token;
  }

  private send(path: string, method: string, body?: unknown, signal?: AbortSignal): Promise<Response> {
    if (!path.startsWith("/") || path.startsWith("//")) throw new Error("A same-origin path is required");
    signal?.throwIfAborted();
    return this.fetcher(path, {method, credentials: "same-origin", redirect: "error", signal,
      headers: method === "POST" ? {"Content-Type": "application/json", "X-GraphMind-CSRF": this.token()} : {},
      ...(body === undefined ? {} : {body: JSON.stringify(body)})});
  }

  private async error(response: Response): Promise<ApiError> {
    let payload: {error?: string; message?: string} = {};
    try { payload = await response.clone().json(); } catch { /* A proxy may return a non-JSON error. */ }
    return new ApiError(payload.error ?? "request_failed", payload.message ?? "Request failed. Please retry.", response.status);
  }

  async refresh(): Promise<void> {
    if (!this.renewal) {
      this.renewal = (async () => {
        const response = await this.send("/auth/refresh", "POST", {});
        if (!response.ok) throw await this.error(response);
      })().finally(() => { this.renewal = null; });
    }
    return this.renewal;
  }

  async request(path: string, method: string, body?: unknown, signal?: AbortSignal): Promise<Response> {
    let response = await this.send(path, method, body, signal);
    // Only authentication_required is returned before reader/model execution.
    // Never replay revoked, failed or timed-out model work.
    if (response.status === 401 && path !== "/auth/refresh") {
      const problem = await this.error(response);
      if (problem.code === "authentication_required") {
        await this.refresh();
        signal?.throwIfAborted();
        response = await this.send(path, method, body, signal);
      }
    }
    if (!response.ok) throw await this.error(response);
    return response;
  }

  async post<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
    return (await this.request(path, "POST", body, signal)).json();
  }
}
