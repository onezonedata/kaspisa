import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { PREFS_SCRIPT } from "@/lib/prefs";

import RootLayout from "./layout";

describe("корневой макет (Т13)", () => {
  it("содержит страницу, подвал выводят страницы", () => {
    const html = renderToStaticMarkup(
      <RootLayout params={Promise.resolve({})}>
        <p>x</p>
      </RootLayout>,
    );
    expect(html).toContain("<p>x</p>");
    expect(html).not.toContain("https://github.com/onezonedata/kaspisa/tree/v0.1.0");
  });

  it("ставит тему до отрисовки и не тянет внешние шрифты (Т2)", () => {
    const html = renderToStaticMarkup(
      <RootLayout params={Promise.resolve({})}>
        <p>x</p>
      </RootLayout>,
    );
    expect(html).toContain('<html lang="ru"');
    const head = /<head>([\s\S]*?)<\/head>/.exec(html)?.[1] ?? "";
    expect(head).toContain(`<script>${PREFS_SCRIPT}</script>`);
    expect(html).not.toContain("fonts.googleapis");
    expect(html).not.toContain("fonts.gstatic");
  });
});
