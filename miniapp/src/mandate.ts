import schema from "./generated/mandate.schema.json";

export interface MeetingState {
  slots: string[];
  timezone: string;
  places: string[];
  excluded: string[];
  time_weight: number;
  reservation: number;
  max_rounds: number;
  confirm_before_agreeing: boolean;
}

export const SENSITIVE_FIELDS = ["time_weight", "reservation"] as const;

type JsonSchema = {
  properties?: Record<string, JsonSchema>;
  required?: string[];
  $defs?: Record<string, JsonSchema>;
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  $ref?: string;
  allOf?: JsonSchema[];
};

const root = schema as JsonSchema;

function definition(name: string): JsonSchema {
  const found = root.$defs?.[name];
  if (found === undefined) throw new Error(`Схема сервера не содержит ${name}`);
  return found;
}

export interface Bounds {
  min: number;
  max: number;
}

export function formBounds(): { reservation: Bounds; maxRounds: Bounds } {
  const reservation = definition("Reservation").properties?.minimum_utility ?? {};
  const rounds = definition("Limits").properties?.max_rounds ?? {};
  return {
    reservation: { min: reservation.minimum ?? 0, max: reservation.maximum ?? 1 },
    maxRounds: {
      min: rounds.exclusiveMinimum !== undefined ? rounds.exclusiveMinimum + 1 : rounds.minimum ?? 1,
      max: 50,
    },
  };
}

export function requiredFields(): string[] {
  return [...(root.required ?? [])];
}

function offsetMinutes(instant: number, timezone: string): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: timezone,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).formatToParts(new Date(instant));
  const value = (type: string): number => Number(parts.find((part) => part.type === type)?.value);
  const local = Date.UTC(
    value("year"),
    value("month") - 1,
    value("day"),
    value("hour"),
    value("minute"),
    value("second"),
  );
  return Math.round((local - instant) / 60000);
}

export function zonedIso(local: string, timezone: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(local);
  if (match === null) throw new Error(`Неверный формат времени: ${local}`);
  const [, year, month, day, hour, minute] = match.map(Number) as number[];
  const wall = Date.UTC(year!, month! - 1, day!, hour!, minute!);
  let offset = offsetMinutes(wall, timezone);
  offset = offsetMinutes(wall - offset * 60000, timezone);
  const sign = offset >= 0 ? "+" : "-";
  const absolute = Math.abs(offset);
  const hh = String(Math.floor(absolute / 60)).padStart(2, "0");
  const mm = String(absolute % 60).padStart(2, "0");
  return `${local}:00${sign}${hh}:${mm}`;
}

function rankValue(rank: number, size: number): number {
  return size === 1 ? 1 : 1 - rank / (size - 1);
}

function points(entries: Array<[number, number]>): Array<[number, number]> {
  const sorted = [...entries].sort((left, right) => left[0] - right[0]);
  if (sorted.length === 1) sorted.push([sorted[0]![0] + 1, sorted[0]![1]]);
  return sorted;
}

export function validate(state: MeetingState): string[] {
  const errors: string[] = [];
  const bounds = formBounds();
  if (state.slots.length === 0) errors.push("Добавьте хотя бы одно время");
  if (new Set(state.slots).size !== state.slots.length) errors.push("Время повторяется");
  if (state.places.length === 0) errors.push("Добавьте хотя бы одно место");
  if (new Set(state.places).size !== state.places.length) errors.push("Место повторяется");
  if (!Number.isInteger(state.time_weight) || state.time_weight < 0 || state.time_weight > 100) {
    errors.push("Вес времени — целое число от 0 до 100");
  }
  if (state.reservation < bounds.reservation.min || state.reservation > bounds.reservation.max) {
    errors.push("Порог согласия вне допустимого диапазона");
  }
  if (state.max_rounds < bounds.maxRounds.min || state.max_rounds > bounds.maxRounds.max) {
    errors.push("Число раундов вне допустимого диапазона");
  }
  return errors;
}

export function buildMandate(state: MeetingState): Record<string, unknown> {
  const errors = validate(state);
  if (errors.length > 0) throw new Error(errors.join("; "));
  const isoSlots = state.slots.map((slot) => zonedIso(slot, state.timezone));
  const chronological = [...isoSlots].sort((left, right) => Date.parse(left) - Date.parse(right));
  const timeWeight = state.time_weight / 100;
  const mandate: Record<string, unknown> = {
    template: "meeting",
    issues: [
      { id: "time", kind: "datetime", choices: chronological },
      { id: "place", kind: "place", choices: [...state.places] },
    ],
    utility: {
      weights: { time: timeWeight, place: Math.round((1 - timeWeight) * 1e9) / 1e9 },
      value_functions: {
        time: {
          points: points(
            isoSlots.map((slot, rank) => [Date.parse(slot) / 1000, rankValue(rank, isoSlots.length)]),
          ),
        },
        place: {
          points: points(state.places.map((_, rank) => [rank, rankValue(rank, state.places.length)])),
        },
      },
    },
    hard_constraints:
      state.excluded.length === 0
        ? []
        : [{ kind: "cmp", issue: "place", op: "not_in", value: [...state.excluded] }],
    reservation: { minimum_utility: state.reservation },
    strategy: { kind: "time_dependent", params: { beta: 0.5 } },
    limits: { max_rounds: state.max_rounds },
    escalation: { always: state.confirm_before_agreeing ? ["accept_deal"] : [] },
  };
  return mandate;
}

export function describeChanges(
  before: Record<string, unknown> | null,
  after: Record<string, unknown>,
): string[] {
  if (before === null) return ["Новый мандат"];
  return Object.keys(after)
    .filter((key) => JSON.stringify(before[key]) !== JSON.stringify(after[key]))
    .map((key) => `Изменено: ${key}`);
}
