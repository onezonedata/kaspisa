// @vitest-environment node
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const tokensCss = readFileSync(resolve(here, "tokens.css"), "utf8");
const globalsCss = readFileSync(resolve(here, "globals.css"), "utf8");

type Vars = Map<string, string>;

function parseVars(body: string): Vars {
  const vars: Vars = new Map();
  for (const m of body.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
    vars.set(m[1], m[2].trim());
  }
  return vars;
}

/** Тело блока с заданным селектором (первая закрывающая скобка — конец блока). */
function blockBody(css: string, selector: string): string {
  const start = css.indexOf(selector);
  if (start < 0) throw new Error(`нет блока ${selector}`);
  const open = css.indexOf("{", start);
  const close = css.indexOf("}", open);
  return css.slice(open + 1, close);
}

const base = parseVars(blockBody(tokensCss, ":root {"));
const darkAttr = parseVars(blockBody(tokensCss, ':root[data-theme="dark"]'));
const mediaStart = tokensCss.indexOf("@media (prefers-color-scheme: dark)");
const darkMedia = parseVars(
  blockBody(tokensCss.slice(mediaStart), ':root:not([data-theme="light"])'),
);

function luminance(hex: string): number {
  const h = hex.replace("#", "");
  const full = h.length === 3 ? [...h].map((c) => c + c).join("") : h;
  const [r, g, b] = [0, 2, 4].map((i) => {
    const c = parseInt(full.slice(i, i + 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

interface Pair {
  fg: string;
  bg: string;
}

function cross(fgs: string[], bgs: string[]): Pair[] {
  return fgs.flatMap((fg) => bgs.map((bg) => ({ fg, bg })));
}

const PAGE_BGS = ["--bg", "--surface", "--surface-2", "--row-selected"];
const TONES = ["--tone-first", "--tone-pending", "--tone-nobounds", "--tone-untracked", "--tone-problem"];
const MODES = ["--mode-off", "--mode-dry", "--mode-live", "--mode-halted"];

const PAIRS: Pair[] = [
  ...cross(["--text", "--text-muted", "--link"], PAGE_BGS),
  ...cross(TONES, ["--surface", "--row-selected"]),
  { fg: "--accent", bg: "--surface" },
  { fg: "--on-accent", bg: "--accent" },
  { fg: "--bulk-text", bg: "--bulk-bg" },
  { fg: "--danger-text", bg: "--danger-bg" },
  { fg: "--warn-text", bg: "--warn-bg" },
  { fg: "--info-text", bg: "--info-bg" },
  ...cross(["--side-text", "--side-muted"], ["--side-bg", "--side-card"]),
  { fg: "--side-text", bg: "--side-active" },
  { fg: "--side-active-text", bg: "--side-active" },
  ...cross(MODES, ["--side-card", "--side-bg"]),
  { fg: "--tooltip-text", bg: "--tooltip-bg" },
];

const isColor = (value: string) => /^(#|rgb)/.test(value);
// Токены меню, подсказки и подложки одинаковы в обеих темах и объявлены один раз.
const THEME_INDEPENDENT = /^--(side-|mode-|tooltip-|backdrop$)/;

describe("токены дизайна (Т5)", () => {
  it("тёмные блоки (атрибут и медиазапрос) совпадают по именам и значениям", () => {
    expect(darkAttr.size).toBeGreaterThan(10);
    expect(Object.fromEntries(darkMedia)).toEqual(Object.fromEntries(darkAttr));
  });

  it("каждый цвет светлой темы переопределён в тёмной, кроме меню, подсказки и подложки", () => {
    const missing = [...base]
      .filter(([name, value]) => isColor(value) && !THEME_INDEPENDENT.test(name))
      .filter(([name]) => !darkAttr.has(name))
      .map(([name]) => name);
    expect(missing).toEqual([]);
  });

  it("цвета пар — hex", () => {
    for (const { fg, bg } of PAIRS) {
      for (const name of [fg, bg]) {
        const value = darkAttr.get(name) ?? base.get(name);
        expect(value, name).toMatch(/^#[0-9a-f]{6}$/i);
        expect(base.get(name), name).toMatch(/^#[0-9a-f]{6}$/i);
      }
    }
  });

  it.each([
    ["светлая", base, base],
    ["тёмная", darkAttr, base],
  ])("контраст пар не ниже 4.5:1, %s тема", (_name, theme, fallback) => {
    const get = (name: string) => theme.get(name) ?? fallback.get(name) ?? "";
    const weak = PAIRS.map(({ fg, bg }) => ({ fg, bg, ratio: contrast(get(fg), get(bg)) })).filter(
      (p) => p.ratio < 4.5,
    );
    expect(weak).toEqual([]);
  });

  it.each([
    ["светлая", base, base],
    ["тёмная", darkAttr, base],
  ])("«границы не заданы» и «отказ» различаются по светлоте не меньше чем на 0.04, %s тема", (_n, theme, fallback) => {
    const get = (name: string) => theme.get(name) ?? fallback.get(name) ?? "";
    const diff = Math.abs(luminance(get("--tone-nobounds")) - luminance(get("--tone-problem")));
    expect(diff).toBeGreaterThanOrEqual(0.04);
  });

  it("каждый токен из color: в globals.css есть в перечне пар", () => {
    const used = new Set(
      [...globalsCss.matchAll(/(?<![-\w])color\s*:\s*var\((--[\w-]+)/g)].map((m) => m[1]),
    );
    expect(used.size).toBeGreaterThan(0);
    const covered = new Set(PAIRS.map((p) => p.fg));
    expect([...used].filter((name) => !covered.has(name))).toEqual([]);
  });
});
