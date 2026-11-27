import { ApiError, type ActiveStatus, type ApiClient } from "./api";
import { OwnerKey, type SigningChallenge } from "./crypto";
import { DraftStore } from "./draft";
import { buildMandate, describeChanges, type MeetingState } from "./mandate";

export type Mode = "sign" | "view";

export interface PendingConfirmation {
  challenge: SigningChallenge;
  title: string;
  details: string[];
}

export interface ConfirmationOutcome {
  status: "confirmed" | "unknown";
  result?: Record<string, unknown>;
  active: ActiveStatus | null;
}

const TITLES: Record<SigningChallenge["purpose"], string> = {
  enroll: "Регистрация ключа владельца",
  mandate: "Публикация мандата",
  delegate: "Делегирование агенту",
  escalation: "Решение по сделке",
  revoke_key: "Отзыв ключа агента",
};

export class MiniApp {
  mode: Mode = "view";
  status: ActiveStatus | null = null;

  constructor(
    private readonly api: ApiClient,
    private readonly key: OwnerKey,
    private readonly drafts: DraftStore,
    private readonly retries: number = 3,
  ) {}

  async init(): Promise<ActiveStatus> {
    this.status = await this.api.active();
    this.mode = this.key.supported ? "sign" : "view";
    return this.status;
  }

  async saveDraft(state: MeetingState): Promise<void> {
    await this.drafts.save(state);
  }

  loadDraft(defaults: MeetingState): Promise<MeetingState> {
    return this.drafts.load(defaults);
  }

  private requireSigning(): void {
    if (this.mode !== "sign") {
      throw new Error("Подпись недоступна: этот клиент Telegram не поддерживает SecureStorage");
    }
  }

  async prepareEnroll(): Promise<PendingConfirmation> {
    this.requireSigning();
    const publicKey = await this.key.create();
    const challenge = await this.api.challenge("enroll", { public_key: publicKey });
    return { challenge, title: TITLES.enroll, details: [`Отпечаток ключа: ${publicKey.slice(0, 16)}…`] };
  }

  async preparePublish(state: MeetingState): Promise<PendingConfirmation> {
    this.requireSigning();
    const body = buildMandate(state);
    const current = this.status?.mandate ?? null;
    const challenge = await this.api.challenge("mandate", { body });
    const version = (current?.version ?? 0) + 1;
    return {
      challenge,
      title: TITLES.mandate,
      details: [`Новая версия: ${version}`, ...describeChanges(current?.body ?? null, body)],
    };
  }

  async prepareCopy(): Promise<PendingConfirmation> {
    this.requireSigning();
    const current = this.status?.mandate;
    if (!current) throw new Error("Нет активного мандата для перевыпуска");
    const challenge = await this.api.challenge("mandate", { copy_active: true });
    return {
      challenge,
      title: TITLES.mandate,
      details: [`Перевыпуск версии ${current.version} с вашей подписью как ${current.version + 1}`],
    };
  }

  async prepare(
    purpose: "delegate" | "escalation" | "revoke_key",
    params: Record<string, unknown> = {},
  ): Promise<PendingConfirmation> {
    this.requireSigning();
    const challenge = await this.api.challenge(purpose, params);
    return { challenge, title: TITLES[purpose], details: Object.entries(params).map(([k, v]) => `${k}: ${String(v)}`) };
  }

  async approve(pending: PendingConfirmation): Promise<ConfirmationOutcome> {
    this.requireSigning();
    const signature = await this.key.signChallenge(pending.challenge);
    for (let attempt = 0; attempt < this.retries; attempt += 1) {
      try {
        const result = await this.api.confirm(pending.challenge.id, signature);
        this.status = await this.refresh();
        return { status: "confirmed", result, active: this.status };
      } catch (error) {
        if (error instanceof ApiError) throw error;
      }
    }
    this.status = await this.refresh();
    return { status: "unknown", active: this.status };
  }

  private async refresh(): Promise<ActiveStatus | null> {
    try {
      return await this.api.active();
    } catch {
      return this.status;
    }
  }
}
