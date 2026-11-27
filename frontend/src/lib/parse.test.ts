import { describe, expect, it } from "vitest";

import { parseIntField } from "./parse";

describe("parseIntField, обязательное поле (Т46)", () => {
  const required = { optional: false };

  it.each([
    ["5", 5],
    [" 5 ", 5],
    ["100000000", 100_000_000],
  ])("%j -> %i", (raw, value) => {
    expect(parseIntField(raw, required)).toEqual({ ok: true, value });
  });

  it("пустое поле — required", () => {
    expect(parseIntField("", required)).toEqual({ ok: false, error: "required" });
    expect(parseIntField("   ", required)).toEqual({ ok: false, error: "required" });
  });

  it.each(["0", "100000001"])("%j вне диапазона", (raw) => {
    expect(parseIntField(raw, required)).toEqual({ ok: false, error: "range" });
  });

  it.each(["1e3", "0x10", "1.5", "-1", "1 000", "abc"])("%j не целое", (raw) => {
    expect(parseIntField(raw, required)).toEqual({ ok: false, error: "invalid" });
  });
});

describe("parseIntField, необязательное поле (Т46)", () => {
  const optional = { optional: true };

  it("пустое и пробелы дают null", () => {
    expect(parseIntField("", optional)).toEqual({ ok: true, value: null });
    expect(parseIntField("   ", optional)).toEqual({ ok: true, value: null });
  });

  it("остальное разбирается так же", () => {
    expect(parseIntField("7", optional)).toEqual({ ok: true, value: 7 });
    expect(parseIntField("1e3", optional)).toEqual({ ok: false, error: "invalid" });
    expect(parseIntField("0", optional)).toEqual({ ok: false, error: "range" });
  });
});
