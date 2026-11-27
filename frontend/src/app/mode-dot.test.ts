import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const css = readFileSync(resolve(__dirname, "globals.css"), "utf8");

function rule(selector: string): string {
  const start = css.indexOf(`${selector} {`);
  expect(start, selector).toBeGreaterThanOrEqual(0);
  return css.slice(start, css.indexOf("}", start));
}

describe("точка режима в меню", () => {
  it("режимы различаются формой, а не только цветом", () => {
    const base = rule(".mode-dot");
    expect(base).toContain("border-radius: 50%");
    expect(rule('[data-mode="dry_run"] .mode-dot')).toContain("border: 2px solid");
    expect(rule('[data-mode="halted"] .mode-dot')).toContain("border-radius: 1px");
    // «Выключен» — чёрточка: у неё своя высота, у «работает» (круг) правила формы нет.
    expect(rule('[data-mode="off"] .mode-dot')).toContain("height: 2px");
    expect(css).not.toContain('[data-mode="live"] .mode-dot');
  });
});
