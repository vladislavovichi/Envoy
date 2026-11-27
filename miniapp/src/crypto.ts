import * as ed from "@noble/ed25519";

import { fromBase64Url, toBase64Url } from "./base64";
import type { KeyValueStore } from "./storage";

export const SEED_KEY = "envoy_owner_seed";

export interface SigningChallenge {
  id: string;
  purpose: "enroll" | "mandate" | "delegate" | "escalation" | "revoke_key";
  bytes_to_sign: string;
  expires_at: string;
}

export async function publicKeyOf(seed: Uint8Array): Promise<Uint8Array> {
  return ed.getPublicKeyAsync(seed);
}

export async function signBytes(seed: Uint8Array, message: Uint8Array): Promise<Uint8Array> {
  return ed.signAsync(message, seed);
}

export class OwnerKey {
  constructor(private readonly secure: KeyValueStore) {}

  get supported(): boolean {
    return this.secure.available;
  }

  private async seed(): Promise<Uint8Array | null> {
    const stored = await this.secure.get(SEED_KEY);
    return stored === null ? null : fromBase64Url(stored);
  }

  async exists(): Promise<boolean> {
    return this.supported && (await this.seed()) !== null;
  }

  async create(): Promise<string> {
    const existing = await this.seed();
    if (existing !== null) return toBase64Url(await publicKeyOf(existing));
    const seed = crypto.getRandomValues(new Uint8Array(32));
    await this.secure.set(SEED_KEY, toBase64Url(seed));
    return toBase64Url(await publicKeyOf(seed));
  }

  async signChallenge(challenge: SigningChallenge): Promise<string> {
    const seed = await this.seed();
    if (seed === null) throw new Error("Ключ владельца не создан на этом устройстве");
    return toBase64Url(await signBytes(seed, fromBase64Url(challenge.bytes_to_sign)));
  }
}
