// Снимки экранов для просмотра человеком (этап 22). Пишутся только при KASPISA_SCREENS=1 и заданном
// абсолютном каталоге KASPISA_SCREENS_DIR; обычный прогон тестов файлов не создаёт и не сравнивает.
import { mkdirSync } from "node:fs";
import path from "node:path";

import { expect, test, type Page } from "@playwright/test";

import { t } from "../src/i18n";
import { FakeApi } from "./fake-api";
import { seedCabinet } from "./seed";

const dir = process.env.KASPISA_SCREENS_DIR ?? "";
const enabled = process.env.KASPISA_SCREENS === "1" && path.isAbsolute(dir);

const sizes = [
  { id: "1280", viewport: { width: 1280, height: 800 }, phone: false },
  { id: "375", viewport: { width: 375, height: 740 }, phone: true },
] as const;
const schemes = ["light", "dark"] as const;

async function settle(page: Page) {
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(300);
}

test.describe("снимки экранов", () => {
  test.skip(!enabled, "нужны KASPISA_SCREENS=1 и абсолютный KASPISA_SCREENS_DIR");
  test.setTimeout(120_000);

  test("сохранить снимки", async ({ browser, baseURL }) => {
    mkdirSync(dir, { recursive: true });
    const origin = new URL(baseURL!).origin;

    for (const scheme of schemes) {
      for (const size of sizes) {
        const context = await browser.newContext({
          baseURL,
          viewport: size.viewport,
          colorScheme: scheme,
          isMobile: size.phone,
          hasTouch: size.phone,
        });
        const page = await context.newPage();
        const fake = new FakeApi();
        await fake.install(page, origin);
        const shopId = seedCabinet(fake);
        const shot = async (name: string) => {
          await settle(page);
          await page.screenshot({
            path: path.join(dir, `${name}-${scheme}-${size.id}.png`),
            fullPage: true,
          });
        };

        await page.goto(`/shops/${shopId}/products`);
        await expect(page.getByRole("row").nth(1)).toBeVisible();
        await shot("products");
        if (!size.phone) {
          // Правая часть таблицы со статусами проверки и отказом.
          await page.evaluate(() => {
            const box = document.querySelector(".table-scroll")!;
            box.scrollLeft = box.scrollWidth;
          });
          await shot("products-scrolled");
          await page.evaluate(() => {
            document.querySelector(".table-scroll")!.scrollLeft = 0;
          });
        }

        await page.goto(`/shops/${shopId}/products/4`);
        await expect(page.getByLabel(t.ui.minPriceLabel)).toBeVisible();
        await shot("product");

        await page.goto(`/shops/${shopId}`);
        await expect(
          page.getByRole("button", { name: t.ui.save, exact: true }).first(),
        ).toBeVisible();
        await shot("settings");

        if (!size.phone && scheme === "light") {
          await page.getByRole("button", { name: t.ui.sidebarCollapse }).click();
          await page.getByRole("link", { name: t.ui.productsLink, exact: true }).hover();
          await shot("menu-collapsed");
        }
        if (size.phone && scheme === "dark") {
          await page.getByRole("button", { name: t.ui.menuOpen }).click();
          await expect(page.getByRole("link", { name: t.ui.productsLink, exact: true })).toBeVisible();
          await page.waitForTimeout(300);
          await page.screenshot({
            path: path.join(dir, `menu-open-${scheme}-${size.id}.png`),
          });
        }

        fake.user = null;
        await page.goto("/login");
        await expect(page.getByRole("button", { name: t.ui.signIn })).toBeVisible();
        await shot("login");

        expect(fake.external).toBe(0);
        await context.close();
      }
    }
  });
});
