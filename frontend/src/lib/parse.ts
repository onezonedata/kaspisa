// Разбор целых из полей ввода: только цифры, 1…100 000 000.
export type ParseResult =
  | { ok: true; value: number | null }
  | { ok: false; error: "required" | "invalid" | "range" };

const MAX = 100_000_000;

export function parseIntField(raw: string, { optional }: { optional: boolean }): ParseResult {
  const s = raw.trim();
  if (s === "") return optional ? { ok: true, value: null } : { ok: false, error: "required" };
  if (!/^[0-9]+$/.test(s)) return { ok: false, error: "invalid" };
  const n = Number(s);
  if (n < 1 || n > MAX) return { ok: false, error: "range" };
  return { ok: true, value: n };
}
