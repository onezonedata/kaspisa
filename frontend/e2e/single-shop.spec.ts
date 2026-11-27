// Сквозной путь Т20: один магазин на установку, на заглушке API.
import { expect, test } from "@playwright/test";

import { t } from "../src/i18n";
import { FakeApi } from "./fake-api";

test("один магазин: форма скрывается, подключение той же записи, удаление возвращает форму", async ({
  page,
  baseURL,
}) => {
  const fake = new FakeApi();
  fake.account = { email: "seller@example.kz", password: "test-password-1" };
  await fake.install(page, new URL(baseURL!).origin);

  await page.goto("/login");
  const login = page.locator("form:visible");
  await login.getByLabel(t.ui.email).fill("seller@example.kz");
  await login.getByLabel(t.ui.password).fill("test-password-1");
  await page.getByRole("button", { name: t.ui.signIn }).click();
  await expect(page).toHaveURL(/\/shops$/);

  // Список пуст: форма видна.
  const add = page.locator("form:visible");
  await add.getByLabel(t.ui.kaspiLogin).fill("shop-login");
  await add.getByLabel(t.ui.kaspiPassword).fill("shop-secret");
  await add.getByRole("button", { name: t.ui.addShop }).click();
  await expect(page).toHaveURL(/\/shops\/[^/]+$/);
  await expect(
    page.locator("strong").filter({ hasText: t.connectionState.not_connected }),
  ).toBeVisible();
  const shopUrl = page.url();

  // Назад к списку: формы нет, магазин без подключения есть.
  await page.goto("/shops");
  await expect(page.getByText(t.ui.shopNoConnection)).toBeVisible();
  await expect(page.getByLabel(t.ui.kaspiPassword)).toHaveCount(0);
  expect(fake.shops).toHaveLength(1);

  // Открыть ту же запись и подключить кодом.
  await page.getByRole("link", { name: t.ui.shopNoConnection }).click();
  await expect(page).toHaveURL(shopUrl);
  await page.getByRole("button", { name: t.ui.connect }).click();
  await page.getByLabel(t.ui.code).fill("123456");
  await page.getByRole("button", { name: t.ui.sendCode }).click();
  await expect(
    page.locator("strong").filter({ hasText: t.connectionState.connected }),
  ).toBeVisible();
  expect(fake.shops).toHaveLength(1);

  // Удалить магазин: форма снова видна.
  page.once("dialog", (dialog) => void dialog.accept());
  await page.getByRole("button", { name: t.ui.deleteShop }).click();
  await expect(page).toHaveURL(/\/shops$/);
  await expect(page.getByLabel(t.ui.kaspiPassword)).toBeVisible();
  expect(fake.shops).toHaveLength(0);

  expect(fake.csrfMissing).toBe(0);
  expect(fake.unhandled).toBe(0);
});
