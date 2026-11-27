import { fromBase64Url, toBase64Url } from "./base64";
import { SENSITIVE_FIELDS, type MeetingState } from "./mandate";
import type { KeyValueStore } from "./storage";

export const DRAFT_KEY = "envoy_meeting_draft";
export const DRAFT_SECRET = "envoy_draft_key";

type Sensitive = Pick<MeetingState, (typeof SENSITIVE_FIELDS)[number]>;
type Public = Omit<MeetingState, (typeof SENSITIVE_FIELDS)[number]>;

export class DraftStore {
  constructor(
    private readonly device: KeyValueStore,
    private readonly secure: KeyValueStore,
  ) {}

  private async key(create: boolean): Promise<CryptoKey | null> {
    if (!this.secure.available) return null;
    let raw = await this.secure.get(DRAFT_SECRET);
    if (raw === null) {
      if (!create) return null;
      raw = toBase64Url(crypto.getRandomValues(new Uint8Array(32)));
      await this.secure.set(DRAFT_SECRET, raw);
    }
    return crypto.subtle.importKey("raw", fromBase64Url(raw), "AES-GCM", false, ["encrypt", "decrypt"]);
  }

  async save(state: MeetingState): Promise<void> {
    if (!this.device.available) return;
    const { time_weight, reservation, ...visible } = state;
    const record: { public: Public; sealed?: string } = { public: visible };
    const key = await this.key(true);
    if (key !== null) {
      const iv = crypto.getRandomValues(new Uint8Array(12));
      const plain = new TextEncoder().encode(JSON.stringify({ time_weight, reservation } satisfies Sensitive));
      const sealed = new Uint8Array(await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key, plain));
      record.sealed = `${toBase64Url(iv)}.${toBase64Url(sealed)}`;
    }
    await this.device.set(DRAFT_KEY, JSON.stringify(record));
  }

  async load(defaults: MeetingState): Promise<MeetingState> {
    if (!this.device.available) return defaults;
    const stored = await this.device.get(DRAFT_KEY);
    if (stored === null) return defaults;
    const record = JSON.parse(stored) as { public: Public; sealed?: string };
    let sensitive: Sensitive = { time_weight: defaults.time_weight, reservation: defaults.reservation };
    const key = record.sealed === undefined ? null : await this.key(false);
    if (key !== null && record.sealed !== undefined) {
      const [iv, data] = record.sealed.split(".");
      try {
        const plain = await crypto.subtle.decrypt(
          { name: "AES-GCM", iv: fromBase64Url(iv ?? "") },
          key,
          fromBase64Url(data ?? ""),
        );
        sensitive = JSON.parse(new TextDecoder().decode(plain)) as Sensitive;
      } catch {
        sensitive = { time_weight: defaults.time_weight, reservation: defaults.reservation };
      }
    }
    return { ...defaults, ...record.public, ...sensitive };
  }
}
