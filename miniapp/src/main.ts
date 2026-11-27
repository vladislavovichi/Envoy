import { ApiClient } from "./api";
import { MiniApp, type PendingConfirmation } from "./app";
import { OwnerKey } from "./crypto";
import { DraftStore } from "./draft";
import { formBounds, validate, type MeetingState } from "./mandate";
import { stores, webApp } from "./telegram";

const DEFAULTS: MeetingState = {
  slots: [],
  timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
  places: [],
  excluded: [],
  time_weight: 50,
  reservation: 0.4,
  max_rounds: 6,
  confirm_before_agreeing: true,
};

function element<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  text?: string,
  attributes: Record<string, string> = {},
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
  return node;
}

function lines(value: string): string[] {
  return value.split("\n").map((line) => line.trim()).filter((line) => line.length > 0);
}

function field(root: HTMLElement, label: string, input: HTMLInputElement | HTMLTextAreaElement): void {
  root.append(element("label", label), input);
}

async function start(): Promise<void> {
  const telegram = webApp();
  telegram?.ready();
  const root = document.getElementById("app")!;
  const { secure, device } = stores(telegram);
  const app = new MiniApp(
    new ApiClient(location.origin, telegram?.initData ?? "", (input, init) => fetch(input, init)),
    new OwnerKey(secure),
    new DraftStore(device, secure),
  );
  const status = await app.init();
  const state = await app.loadDraft(DEFAULTS);
  const bounds = formBounds();
  root.replaceChildren();
  root.append(element("h2", "Мандат встречи"));
  if (app.mode === "view") {
    root.append(
      element(
        "p",
        "Этот клиент Telegram не поддерживает защищённое хранилище: доступны просмотр и черновик, подпись — в обновлённом клиенте.",
        { class: "notice" },
      ),
    );
  }
  root.append(
    element(
      "p",
      status.mandate
        ? `Активна версия ${status.mandate.version} (${status.mandate.attestation === "owner" ? "подписана вами" : "аттестация платформы"}).`
        : "Активного мандата нет.",
      { class: "hint" },
    ),
  );
  const slots = element("textarea", state.slots.join("\n"), { rows: "4", placeholder: "2026-12-03T10:00" });
  const timezone = element("input", undefined, { value: state.timezone });
  const places = element("textarea", state.places.join("\n"), { rows: "3" });
  const excluded = element("textarea", state.excluded.join("\n"), { rows: "2" });
  const weight = element("input", undefined, { type: "number", min: "0", max: "100", value: String(state.time_weight) });
  const reservation = element("input", undefined, {
    type: "number",
    step: "0.05",
    min: String(bounds.reservation.min),
    max: String(bounds.reservation.max),
    value: String(state.reservation),
  });
  const rounds = element("input", undefined, {
    type: "number",
    min: String(bounds.maxRounds.min),
    max: String(bounds.maxRounds.max),
    value: String(state.max_rounds),
  });
  const confirm = element("input", undefined, { type: "checkbox" });
  confirm.checked = state.confirm_before_agreeing;
  field(root, "Время по приоритету (по строке, местное)", slots);
  field(root, "Часовой пояс", timezone);
  field(root, "Места по приоритету", places);
  field(root, "Исключить места", excluded);
  field(root, "Важность времени, % (остальное — место)", weight);
  field(root, "Порог согласия (0–1)", reservation);
  field(root, "Раундов не больше", rounds);
  root.append(element("label", "Спрашивать меня перед согласием"), confirm);
  const messages = element("div", undefined, { class: "hint" });
  const panel = element("section");
  root.append(messages, panel);

  const current = (): MeetingState => ({
    slots: lines(slots.value),
    timezone: timezone.value.trim(),
    places: lines(places.value),
    excluded: lines(excluded.value),
    time_weight: Number(weight.value),
    reservation: Number(reservation.value),
    max_rounds: Number(rounds.value),
    confirm_before_agreeing: confirm.checked,
  });

  const show = (pending: PendingConfirmation): void => {
    panel.replaceChildren(element("h3", pending.title), ...pending.details.map((line) => element("p", line)));
    const sign = element("button", "Подписать на этом устройстве");
    const cancel = element("button", "Отмена", { class: "secondary" });
    sign.onclick = async () => {
      sign.disabled = true;
      try {
        const outcome = await app.approve(pending);
        messages.textContent =
          outcome.status === "confirmed"
            ? "Подписано и принято сервером."
            : "Связь прервалась: показан фактический статус с сервера.";
        panel.replaceChildren(element("pre", JSON.stringify(outcome.active, null, 2)));
      } catch (error) {
        messages.textContent = String(error);
        sign.disabled = false;
      }
    };
    cancel.onclick = () => panel.replaceChildren();
    panel.append(sign, cancel);
  };

  const action = (label: string, run: () => Promise<PendingConfirmation>, secondary = false): void => {
    const button = element("button", label, secondary ? { class: "secondary" } : {});
    button.onclick = async () => {
      messages.textContent = "";
      try {
        show(await run());
      } catch (error) {
        messages.textContent = String(error);
      }
    };
    root.append(button);
  };

  const save = element("button", "Сохранить черновик", { class: "secondary" });
  save.onclick = async () => {
    const errors = validate(current());
    await app.saveDraft(current());
    messages.textContent = errors.length ? errors.join("; ") : "Черновик сохранён на устройстве.";
  };
  root.append(save);
  if (app.mode === "sign") {
    if (!status.enrolled) action("Создать ключ владельца", () => app.prepareEnroll());
    action("Подписать и опубликовать", () => app.preparePublish(current()));
    if (status.mandate?.attestation === "platform") action("Перевыпустить с моей подписью", () => app.prepareCopy(), true);
    action("Делегировать агенту", () => app.prepare("delegate"), true);
    if (status.delegation) action("Отозвать ключ агента", () => app.prepare("revoke_key"), true);
  }
}

void start();
