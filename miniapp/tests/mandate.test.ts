import { describe, expect, it } from "vitest";

import { ApiClient, type Fetch } from "../src/api";
import { MiniApp } from "../src/app";
import { fromBase64Url, toBase64Url } from "../src/base64";
import { OwnerKey, SEED_KEY, publicKeyOf, signBytes } from "../src/crypto";
import { DRAFT_KEY, DraftStore } from "../src/draft";
import { buildMandate, formBounds, requiredFields, zonedIso, type MeetingState } from "../src/mandate";
import { MemoryStore } from "../src/storage";
import schema from "../src/generated/mandate.schema.json";
import vectors from "./fixtures/vectors.json";

const STATE = vectors.meeting.state as MeetingState;

interface Recorded {
  url: string;
  body: string;
  headers: string;
}

class FakeServer {
  readonly requests: Recorded[] = [];
  readonly redeemed = new Map<string, string>();
  active = { enrolled: false, mandate: null as null | Record<string, unknown>, delegation: null };
  dropNextConfirm = false;
  private counter = 0;

  fetch: Fetch = async (url, init) => {
    const body = String(init?.body ?? "");
    this.requests.push({ url, body, headers: JSON.stringify(init?.headers ?? {}) });
    const path = new URL(url).pathname;
    if (path === "/miniapp/mandates/active") return this.json(this.active);
    if (path === "/miniapp/challenges") {
      const parsed = JSON.parse(body) as { purpose: string };
      this.counter += 1;
      return this.json({
        id: `c${this.counter}`,
        purpose: parsed.purpose,
        bytes_to_sign: toBase64Url(new TextEncoder().encode(`challenge-${this.counter}`)),
        expires_at: "2026-12-01T12:10:00+00:00",
      });
    }
    const match = /\/miniapp\/challenges\/(.+)\/signature/.exec(path);
    if (match) {
      const id = match[1]!;
      const signature = (JSON.parse(body) as { signature: string }).signature;
      const previous = this.redeemed.get(id);
      if (previous !== undefined && previous !== signature) return this.json({ error: "challenge_used" }, 409);
      this.redeemed.set(id, signature);
      this.active = { ...this.active, enrolled: true };
      if (this.dropNextConfirm) {
        this.dropNextConfirm = false;
        throw new TypeError("network connection lost");
      }
      return this.json({ enrolled: true });
    }
    return this.json({ error: "not_found" }, 404);
  };

  private json(value: unknown, status = 200): Response {
    return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
  }
}

function build(secure = new MemoryStore(), device = new MemoryStore()) {
  const server = new FakeServer();
  const app = new MiniApp(
    new ApiClient("https://envoy.example", "query_id=1&hash=abc", server.fetch),
    new OwnerKey(secure),
    new DraftStore(device, secure),
  );
  return { server, app, secure, device };
}

describe("shared vectors", () => {
  it("signs the Python Ed25519 vector byte for byte", async () => {
    const seed = fromBase64Url(vectors.ed25519.seed);
    expect(toBase64Url(await publicKeyOf(seed))).toBe(vectors.ed25519.public_key);
    const signature = await signBytes(seed, fromBase64Url(vectors.ed25519.message));
    expect(toBase64Url(signature)).toBe(vectors.ed25519.signature);
  });

  it("builds the meeting mandate the server validates", () => {
    expect(buildMandate(STATE)).toEqual(vectors.meeting.mandate);
  });

  it("derives the form from the generated server schema", () => {
    const built = buildMandate(STATE);
    for (const field of requiredFields()) expect(built).toHaveProperty(field);
    const properties = Object.keys((schema as { properties: Record<string, unknown> }).properties);
    for (const key of Object.keys(built)) expect(properties).toContain(key);
    expect(formBounds().reservation).toEqual({ min: 0, max: 1 });
    expect(formBounds().maxRounds.min).toBe(1);
  });

  it("converts local slots with the named time zone", () => {
    expect(zonedIso("2026-07-01T10:00", "Europe/Berlin")).toBe("2026-07-01T10:00:00+02:00");
    expect(zonedIso("2026-12-01T10:00", "Europe/Berlin")).toBe("2026-12-01T10:00:00+01:00");
    expect(() => buildMandate({ ...STATE, slots: [] })).toThrow();
  });
});

describe("owner signing", () => {
  it("never sends the private key and signs challenge bytes", async () => {
    const { server, app, secure } = build();
    await app.init();
    expect(app.mode).toBe("sign");
    const enroll = await app.prepareEnroll();
    const outcome = await app.approve(enroll);
    expect(outcome.status).toBe("confirmed");
    await app.approve(await app.preparePublish(STATE));
    const seed = secure.values.get(SEED_KEY)!;
    const seedHex = [...fromBase64Url(seed)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
    const traffic = JSON.stringify(server.requests);
    expect(traffic).not.toContain(seed);
    expect(traffic).not.toContain(seedHex);
    const publicKey = JSON.parse(server.requests.find((r) => r.url.endsWith("/challenges"))!.body).params.public_key;
    expect(publicKey).toBe(toBase64Url(await publicKeyOf(fromBase64Url(seed))));
  });

  it("keeps view and draft mode when SecureStorage is unsupported", async () => {
    const device = new MemoryStore();
    const { app } = build(new MemoryStore(false), device);
    await app.init();
    expect(app.mode).toBe("view");
    await expect(app.prepareEnroll()).rejects.toThrow("SecureStorage");
    await expect(app.preparePublish(STATE)).rejects.toThrow("SecureStorage");
    await app.saveDraft({ ...STATE, reservation: 0.4321, time_weight: 37 });
    const stored = device.values.get(DRAFT_KEY)!;
    expect(stored).not.toContain("0.4321");
    expect(stored).not.toContain("37");
    expect((await app.loadDraft(STATE)).places).toEqual(STATE.places);
  });

  it("stores sensitive draft fields only encrypted", async () => {
    const { app, device } = build();
    await app.saveDraft({ ...STATE, reservation: 0.4321, time_weight: 37 });
    const stored = device.values.get(DRAFT_KEY)!;
    expect(stored).toContain("кафе");
    expect(stored).not.toContain("0.4321");
    expect(stored).not.toContain("time_weight");
    const loaded = await app.loadDraft({ ...STATE, reservation: 0.1, time_weight: 1 });
    expect(loaded.reservation).toBe(0.4321);
    expect(loaded.time_weight).toBe(37);
  });

  it("shows the actual server status after the connection drops post-signature", async () => {
    const { server, app } = build();
    await app.init();
    const pending = await app.prepareEnroll();
    server.dropNextConfirm = true;
    const outcome = await app.approve(pending);
    expect(outcome.status).toBe("confirmed");
    expect(outcome.active?.enrolled).toBe(true);
    const confirms = server.requests.filter((r) => r.url.endsWith("/signature"));
    expect(confirms).toHaveLength(2);
    expect(new Set(confirms.map((r) => r.body)).size).toBe(1);
  });

  it("reports unknown status when every retry fails and refreshes from the server", async () => {
    const { server, app } = build();
    await app.init();
    const pending = await app.prepareEnroll();
    const original = server.fetch;
    server.fetch = async (url, init) => {
      if (url.endsWith("/signature")) throw new TypeError("offline");
      return original(url, init);
    };
    const failing = new MiniApp(
      new ApiClient("https://envoy.example", "x", server.fetch),
      new OwnerKey((app as unknown as { key: { secure: MemoryStore } }).key.secure),
      new DraftStore(new MemoryStore(), new MemoryStore()),
    );
    await failing.init();
    const outcome = await failing.approve(pending);
    expect(outcome.status).toBe("unknown");
    expect(outcome.active).not.toBeNull();
  });
});
