// @vitest-environment node
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const srcDir = resolve(here, "..");

function walk(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    return entry.isDirectory() ? walk(path) : [path];
  });
}

const files = walk(srcDir);
const cssFiles = files.filter((f) => f.endsWith(".css"));
// Тесты сами называют запрещённые адреса, поэтому в проверку кода не входят.
const codeFiles = files.filter((f) => /\.(ts|tsx|css)$/.test(f) && !/\.test\.tsx?$/.test(f));
const read = (path: string) => readFileSync(path, "utf8");

describe("стили без внешних адресов и литералов цвета (Т6)", () => {
  it("в CSS нет внешних url() и @import", () => {
    expect(cssFiles.length).toBeGreaterThan(0);
    for (const file of cssFiles) {
      const css = read(file);
      expect(css, file).not.toMatch(/url\(\s*["']?\s*(https?:|\/\/)/i);
      expect(css, file).not.toMatch(/@import\s+(url\(\s*)?["']?\s*(https?:|\/\/)/i);
    }
  });

  it("в коде нет адресов Google Fonts", () => {
    for (const file of codeFiles) {
      const text = read(file);
      expect(text, file).not.toContain("fonts.googleapis");
      expect(text, file).not.toContain("fonts.gstatic");
    }
  });

  it("globals.css берёт цвета только из токенов", () => {
    const css = read(resolve(here, "globals.css")).replace(/\/\*[\s\S]*?\*\//g, "");
    expect(css).not.toMatch(/#[0-9a-f]{3,8}\b/i);
    expect(css).not.toMatch(/\b(rgb|rgba|hsl|hsla|hwb|oklch|oklab|lab|lch)\(/i);
    const values = [...css.matchAll(/[\w-]+\s*:\s*([^;{}]+)[;}]/g)].map((m) => m[1]);
    const named = /\b(white|black|red|green|blue|gray|grey|yellow|orange|purple|navy|teal)\b/i;
    expect(values.filter((v) => named.test(v))).toEqual([]);
  });
});
