import type { SigningChallenge } from "./crypto";

export interface ActiveStatus {
  enrolled: boolean;
  mandate: { id: string; version: number; attestation: string; body: Record<string, unknown> } | null;
  delegation: { mandate_id: string; version: number; expires_at: string } | null;
}

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly reason: string,
  ) {
    super(`API ${status}: ${reason}`);
  }
}

export type Fetch = (input: string, init?: RequestInit) => Promise<Response>;

export class ApiClient {
  constructor(
    private readonly base: string,
    private readonly initData: string,
    private readonly fetchImpl: Fetch,
  ) {}

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await this.fetchImpl(`${this.base}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        Authorization: `tma ${this.initData}`,
        ...(init.headers ?? {}),
      },
    });
    const body = (await response.json().catch(() => ({}))) as Record<string, unknown>;
    if (!response.ok) throw new ApiError(response.status, String(body.error ?? response.statusText));
    return body as T;
  }

  schema(): Promise<Record<string, unknown>> {
    return this.request("/miniapp/mandates/schema");
  }

  active(): Promise<ActiveStatus> {
    return this.request("/miniapp/mandates/active");
  }

  challenge(purpose: SigningChallenge["purpose"], params: Record<string, unknown>): Promise<SigningChallenge> {
    return this.request("/miniapp/challenges", {
      method: "POST",
      body: JSON.stringify({ purpose, params }),
    });
  }

  confirm(id: string, signature: string): Promise<Record<string, unknown>> {
    return this.request(`/miniapp/challenges/${encodeURIComponent(id)}/signature`, {
      method: "POST",
      body: JSON.stringify({ signature }),
    });
  }
}
