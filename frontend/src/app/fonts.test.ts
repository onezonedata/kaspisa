// @vitest-environment node
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const read = (path: string) => readFileSync(path, "utf8");

const pkg = JSON.parse(read(resolve(root, "package.json"))) as {
  dependencies: Record<string, string>;
};
const lock = JSON.parse(read(resolve(root, "package-lock.json"))) as {
  packages: Record<string, { hasInstallScript?: boolean; dependencies?: unknown }>;
};

const SANS = "@fontsource/ibm-plex-sans";
const MONO = "@fontsource/ibm-plex-mono";
const SUBSETS = ["latin", "latin-ext", "cyrillic", "cyrillic-ext"];
const FACES: { family: string; pkg: string; weights: number[] }[] = [
  { family: "IBM Plex Sans", pkg: SANS, weights: [400, 500, 600] },
  { family: "IBM Plex Mono", pkg: MONO, weights: [400, 500] },
];

interface Face {
  family: string;
  weight: string;
  display: string;
  src: string;
  range: string;
}

function parseFaces(css: string): Face[] {
  const blocks = css.match(/@font-face\s*\{[^}]*\}/g) ?? [];
  return blocks.map((block) => {
    const get = (name: string) =>
      new RegExp(`${name}\\s*:\\s*([^;]+);`).exec(block)?.[1].trim() ?? "";
    return {
      family: get("font-family").replace(/["']/g, ""),
      weight: get("font-weight"),
      display: get("font-display"),
      src: get("src"),
      range: get("unicode-range").replace(/\s+/g, ""),
    };
  });
}

/** Подмножество и путь к файлу из `src` блока. */
function fileOf(face: Face): { subset: string; path: string; urls: number } {
  const urls = face.src.match(/url\(/g)?.length ?? 0;
  const url = /url\(\s*["']?([^"')]+)["']?\s*\)/.exec(face.src)?.[1] ?? "";
  const subset = /-(latin-ext|latin|cyrillic-ext|cyrillic)-\d+-normal\.woff2$/.exec(url)?.[1] ?? "";
  const path = url.startsWith("@")
    ? resolve(root, "node_modules", url)
    : resolve(here, url);
  return { subset, path, urls };
}

/** unicode-range блока подмножества из готового CSS пакета. */
function packageRange(pkgName: string, weight: number, subset: string): string {
  const css = read(resolve(root, "node_modules", pkgName, `${weight}.css`));
  const name = pkgName.split("/")[1];
  const marker = `/* ${name}-${subset}-${weight}-normal */`;
  const rest = css.slice(css.indexOf(marker) + marker.length);
  const match = /unicode-range\s*:\s*([^;]+);/.exec(rest);
  return match ? match[1].replace(/\s+/g, "") : "";
}

describe("шрифты (Т1)", () => {
  const css = read(resolve(here, "fonts.css"));
  const faces = parseFaces(css);

  it("зависимости закреплены точно, без скриптов установки и телеметрии IBM", () => {
    expect(pkg.dependencies[SANS]).toBe("5.3.0");
    expect(pkg.dependencies[MONO]).toBe("5.3.0");
    for (const name of [SANS, MONO]) {
      const entry = lock.packages[`node_modules/${name}`];
      expect(entry).toBeDefined();
      expect(entry.hasInstallScript).toBeUndefined();
      expect(entry.dependencies).toBeUndefined();
    }
    const names = Object.keys(lock.packages);
    expect(names.filter((n) => n.includes("@ibm/plex") || n.includes("@ibm/telemetry-js"))).toEqual(
      [],
    );
  });

  it("fonts.css: без @import и без запрещённых подмножеств и форматов", () => {
    expect(css).not.toContain("@import");
    expect(css).not.toContain("greek");
    expect(css).not.toContain("vietnamese");
    expect(css).not.toMatch(/\.woff["')]/);
    expect(css).not.toContain("format(\"woff\")");
  });

  it("fonts.css: ровно 20 блоков — 5 начертаний × 4 подмножества", () => {
    expect(faces).toHaveLength(20);
    const found = faces.map((f) => `${f.family}|${f.weight}|${fileOf(f).subset}`).sort();
    const expected = FACES.flatMap((s) =>
      s.weights.flatMap((w) => SUBSETS.map((sub) => `${s.family}|${w}|${sub}`)),
    ).sort();
    expect(found).toEqual(expected);
  });

  it("каждый блок: swap, один woff2-файл, который существует; unicode-range как в пакете", () => {
    for (const face of faces) {
      const spec = FACES.find((s) => s.family === face.family);
      expect(spec).toBeDefined();
      const { subset, path, urls } = fileOf(face);
      expect(face.display).toBe("swap");
      expect(urls).toBe(1);
      expect(face.src).toContain('format("woff2")');
      expect(path.endsWith(".woff2")).toBe(true);
      expect(path).toContain(`/node_modules/${spec!.pkg}/files/`);
      expect(existsSync(path), path).toBe(true);
      expect(face.range).not.toBe("");
      expect(face.range).toBe(packageRange(spec!.pkg, Number(face.weight), subset));
    }
  });

  it("лицензии OFL побайтно равны LICENSE пакетов", () => {
    const pairs: [string, string][] = [
      [SANS, "IBM-Plex-Sans-OFL.txt"],
      [MONO, "IBM-Plex-Mono-OFL.txt"],
    ];
    for (const [name, file] of pairs) {
      const original = readFileSync(resolve(root, "node_modules", name, "LICENSE"));
      const copy = readFileSync(resolve(root, "public/fonts", file));
      expect(copy.equals(original)).toBe(true);
      expect(copy.toString("utf8")).toContain("SIL Open Font License");
    }
  });
});
