// Сквозной путь Т42: «Узнать цены» у не отслеживаемого товара, на заглушке API.
import { expect, test } from "@playwright/test";

import { t } from "../src/i18n";
import { formatPrice } from "../src/lib/format";
import { makeProduct, makeShop } from "../src/test/fixtures";
import { FakeApi } from "./fake-api";

test("узнать цены: чтение, карточка, повтор отклонён", async ({ page, baseURL }) => {
  const fake = new FakeApi();
  fake.user = { id: "00000000-0000-4000-8000-000000000001", email: "seller@example.kz" };
  const shop = makeShop({ connection_state: "connected", merchant_uid: "30000001", city_id: "750000000" });
  fake.shops = [shop];
  fake.products = [makeProduct({ id: 1, shop_id: shop.id, sku: "SKU-1", title: "Товар 1" })];
  await fake.install(page, new URL(baseURL!).origin);

  await page.goto(`/shops/${shop.id}/products`);
  const row = page.locator("tr", { hasText: "SKU-1" });
  let release!: () => void;
  fake.priceReadGate = new Promise<void>((resolve) => (release = resolve));
  await row.getByRole("button", { name: t.ui.priceRead }).click();
  await expect(row.getByRole("button", { name: t.ui.priceReading })).toBeDisabled();
  release();
  // В строке появились цена и позиция; под кнопкой итога нет.
  await expect(row.getByRole("cell", { name: formatPrice(2998), exact: true }).first()).toBeVisible();
  await expect(row.getByRole("cell", { name: "3", exact: true })).toBeVisible();
  await expect(row.getByRole("button", { name: t.ui.priceRead })).toBeEnabled();

  // Карточка: цифры из ответа, таблицы продавцов нет.
  await row.getByRole("link", { name: "Товар 1" }).click();
  await expect(page).toHaveURL(/\/products\/1$/);
  await expect(page.getByRole("heading", { name: t.ui.priceReadTitle })).toBeVisible();
  await expect(page.locator("[data-field='position']")).toHaveText("3");
  await expect(page.locator("[data-field='current_price']")).toHaveText(formatPrice(2998));

  // Повтор сразу — отказ.
  await page.getByRole("button", { name: t.ui.priceRead }).click();
  await expect(page.locator("p.error:visible")).toContainText(t.errors.price_read_too_early);

  expect(fake.csrfMissing).toBe(0);
  expect(fake.unhandled).toBe(0);
});
