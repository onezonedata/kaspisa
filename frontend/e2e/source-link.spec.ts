// Т17: ссылка «Исходный код» с версией видна на странице входа, регистрации, сброса и в кабинете.
import { expect, test } from "@playwright/test";

import { t } from "../src/i18n";
import { SOURCE_HREF } from "../src/lib/source";
import { FakeApi } from "./fake-api";

const HREF = "https://github.com/onezonedata/kaspisa/tree/v0.2.0";

async function expectFooter(page: import("@playwright/test").Page) {
  const link = page.getByRole("link", { name: t.ui.sourceCode });
  await expect(link).toBeVisible();
  await expect(link).toHaveAttribute("href", SOURCE_HREF);
  await expect(link).toHaveAttribute("href", HREF);
  await expect(page.locator("footer:visible")).toContainText("0.2.0");
}

test("ссылка на исходный код видна на каждой странице", async ({ page, baseURL }) => {
  const fake = new FakeApi();
  fake.account = { email: "seller@example.kz", password: "test-password-1" };
  await fake.install(page, new URL(baseURL!).origin);

  for (const path of ["/login", "/register", "/reset-password"]) {
    await page.goto(path);
    await expectFooter(page);
  }

  await page.goto("/net-takoy-stranicy");
  await expectFooter(page);

  await page.goto("/login");
  const login = page.locator("form:visible");
  await login.getByLabel(t.ui.email).fill("seller@example.kz");
  await login.getByLabel(t.ui.password).fill("test-password-1");
  await page.getByRole("button", { name: t.ui.signIn }).click();
  await expect(page).toHaveURL(/\/shops$/);
  await expectFooter(page);
});
