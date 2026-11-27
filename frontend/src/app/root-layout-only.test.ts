import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

function walk(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    return entry.isDirectory() ? walk(path) : [path];
  });
}

describe("единственный корневой макет (Т22)", () => {
  it("тег html только в src/app/layout.tsx", () => {
    const files = walk(__dirname).filter((f) => /(^|\/)(layout|page)\.tsx$/.test(f));
    const withHtml = files
      .filter((f) => readFileSync(f, "utf8").includes("<html"))
      .map((f) => relative(__dirname, f));
    expect(files.length).toBeGreaterThan(3);
    expect(withHtml).toEqual(["layout.tsx"]);
  });
});
