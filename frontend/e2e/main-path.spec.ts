// Сквозной путь Т42: вход → подключение магазина → настройка товара → журнал, на заглушке API.
import { expect, test } from "@playwright/test";

import { fill, t } from "../src/i18n";
import { formatDateTime, formatPrice } from "../src/lib/format";
import { makePriceChange } from "../src/test/fixtures";
import { FakeApi } from "./fake-api";

test("регистрация открывает кабинет", async ({ page, baseURL }) => {
  const fake = new FakeApi();
  await fake.install(page, new URL(baseURL!).origin);

  await page.goto("/register");
  // Скрытые страницы остаются в DOM (Activity), поэтому берём только видимую форму.
  const register = page.locator("form:visible");
  await register.getByLabel(t.ui.email).fill("new-seller@example.kz");
  await register.getByLabel(t.ui.password).fill("test-password-1");
  await page.getByRole("button", { name: t.ui.registerAction }).click();
  await expect(page).toHaveURL(/\/shops$/);
  await expect(page.getByText("new-seller@example.kz")).toBeVisible();
  expect(fake.csrfMissing).toBe(0);
});

test("регистрация закрыта", async ({ page, baseURL }) => {
  const fake = new FakeApi();
  fake.account = { email: "owner@example.kz", password: "test-password-1" };
  await fake.install(page, new URL(baseURL!).origin);

  await page.goto("/register");
  const register = page.locator("form:visible");
  await register.getByLabel(t.ui.email).fill("staff@example.kz");
  await register.getByLabel(t.ui.password).fill("test-password-1");
  await page.getByRole("button", { name: t.ui.registerAction }).click();
  await expect(page.getByText(t.ui.registrationClosed)).toBeVisible();
  await expect(page.getByRole("button", { name: t.ui.registerAction })).toHaveCount(0);
  await page.getByRole("link", { name: t.ui.toLogin }).click();
  await expect(page).toHaveURL(/\/login$/);
  expect(fake.user).toBeNull();
});

test("вход, магазин, товар, журнал, выход", async ({ page, baseURL }) => {
  const fake = new FakeApi();
  fake.account = { email: "seller@example.kz", password: "test-password-1" };
  await fake.install(page, new URL(baseURL!).origin);

  await page.goto("/");
  await expect(page).toHaveURL(/\/login$/);

  const login = page.locator("form:visible");
  await login.getByLabel(t.ui.email).fill("seller@example.kz");
  await login.getByLabel(t.ui.password).fill("test-password-1");
  await page.getByRole("button", { name: t.ui.signIn }).click();
  await expect(page).toHaveURL(/\/shops$/);

  // Магазин: выдуманные доступы, подключение кодом.
  const add = page.locator("form:visible");
  await add.getByLabel(t.ui.kaspiLogin).fill("shop-login");
  await add.getByLabel(t.ui.kaspiPassword).fill("shop-secret");
  await add.getByRole("button", { name: t.ui.addShop }).click();
  await expect(page).toHaveURL(/\/shops\/[^/]+$/);
  await expect(page.locator("strong").filter({ hasText: t.connectionState.not_connected })).toBeVisible();
  await expect(page.getByText(t.ui.connectHint)).toBeVisible();

  await page.getByRole("button", { name: t.ui.connect }).click();
  await page.getByLabel(t.ui.code).fill("123456");
  await page.getByRole("button", { name: t.ui.sendCode }).click();
  await expect(page.locator("strong").filter({ hasText: t.connectionState.connected })).toBeVisible();
  await expect(
    page.getByText(fill(t.ui.botModeStatus, { mode: t.botMode.off }), { exact: true }),
  ).toBeVisible();

  // Каталог: заглушка держит ответ, пока тест не разрешит syncGate.
  let release!: () => void;
  fake.syncGate = new Promise<void>((resolve) => (release = resolve));
  const syncButton = page.getByRole("button", { name: t.ui.syncing });
  await page.getByRole("button", { name: t.ui.syncCatalog }).click();
  await expect(syncButton).toBeVisible();
  await expect(syncButton).toBeDisabled();
  release();
  await expect(
    page.getByText(fill(t.ui.syncDone, { added: 3, updated: 0, missing: 0 })),
  ).toBeVisible();
  await expect(page.getByText(/Товаров в каталоге: 3,/)).toBeVisible();

  // Товар 3 уже отслеживается ботом: последнее решение — «цена вне границ» (пишет обработчик).
  const checkedAt = "2026-11-15T10:00:00Z";
  const checkedText = fill(t.ui.lastCheck, { time: formatDateTime(checkedAt) });
  Object.assign(fake.products[2], {
    tracked: true,
    min_price: 1000,
    max_price: 2000,
    decision_reason: "out_of_bounds",
    last_checked_at: checkedAt,
  });
  fake.priceChanges.push(
    makePriceChange({
      id: 100,
      product_id: 3,
      sku: "SKU-3",
      title: "Товар 3",
      old_price: 2500,
      new_price: 2500,
      reason: "out_of_bounds",
      target_price: null,
      result: "no_change",
    }),
  );

  // Товары: отслеживаемый SKU-3 наверху с причиной; включить отслеживание у SKU-1.
  await page.getByRole("link", { name: t.ui.productsLink, exact: true }).click();
  await expect(page).toHaveURL(/\/products$/);
  const skuColumn = () => page.getByRole("row").locator("td:nth-child(3)");
  await expect(skuColumn()).toHaveText(["SKU-3", "SKU-1", "SKU-2"]);
  const first = page.getByRole("row").nth(1);
  await expect(first).toContainText("SKU-3");
  await expect(first).toContainText(t.ui.trackedOn);
  await expect(first).toContainText(t.reason.out_of_bounds);
  await expect(first).toContainText(checkedText);
  const track = page.getByRole("switch", { name: fill(t.ui.trackToggle, { sku: "SKU-1" }) });
  const row1 = page.getByRole("row").filter({ hasText: "SKU-1" });
  await expect(row1).toContainText(t.ui.trackedOff);
  // Выключатель управляется ответом сервера, поэтому click, а не check.
  await track.click();
  await expect(track).toBeChecked();
  await expect(row1).toContainText(t.ui.trackedOn);
  await expect(row1).toContainText(t.ui.noBoundsShort);
  await expect(page.getByText(t.ui.orderHint)).toBeVisible();
  await page.getByRole("button", { name: t.ui.refresh }).click();
  await expect(page.getByText(t.ui.orderHint)).toBeHidden();
  // После «Обновить» — порядок сервера: отслеживаемые первыми, внутри по id (SKU-1 теперь отслеживается).
  await expect(skuColumn()).toHaveText(["SKU-1", "SKU-3", "SKU-2"]);
  // Карточка SKU-3: причина и время последней проверки.
  await page.getByRole("link", { name: "Товар 3" }).click();
  await expect(page).toHaveURL(/\/products\/3$/);
  const check = page.locator("dd").filter({ hasText: t.reason.out_of_bounds });
  await expect(check).toContainText(checkedText);
  await page.getByRole("link", { name: t.ui.toProducts }).click();
  await page.getByRole("link", { name: "Товар 1" }).click();
  await expect(page).toHaveURL(/\/products\/1$/);
  await page.getByLabel(t.ui.minPriceLabel).fill("1000");
  await page.getByLabel(t.ui.maxPriceLabel).fill("2000");
  await page.getByRole("button", { name: t.ui.save, exact: true }).click();
  await expect(page.getByText(t.ui.saved)).toBeVisible();

  // Возврат к товарам: у SKU-1 границы заданы, пометки нет.
  await page.getByRole("link", { name: t.ui.toProducts }).click();
  await expect(page.getByRole("row").filter({ hasText: "SKU-1" })).not.toContainText(
    t.ui.noBoundsShort,
  );

  // Страница магазина: два отслеживаемых; бот включается в настройках.
  await page.getByRole("link", { name: t.ui.toShop }).click();
  await expect(page.getByRole("link", { name: fill(t.ui.trackedCount, { n: 2 }) })).toBeVisible();
  // Настройки: настройки «если не хватает минимальной цены» нет, список только у стратегии.
  await expect(page.getByText("Если конкуренты ниже минимальной цены")).toHaveCount(0);
  await expect(page.locator("form:visible select")).toHaveCount(1);
  await page.getByLabel(t.botMode.dry_run).check();
  await page.getByRole("button", { name: t.ui.save, exact: true }).click();
  await expect(page.getByText(t.ui.saved)).toBeVisible();
  await expect(
    page.getByText(fill(t.ui.botModeStatus, { mode: t.botMode.dry_run }), { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(fill(t.ui.botModeStatus, { mode: t.botMode.off }), { exact: true }),
  ).toBeHidden();
  await page.getByRole("link", { name: t.ui.navPriceJournal, exact: true }).click();
  await expect(page).toHaveURL(/\/journal$/);
  const row = page.getByRole("row").filter({ hasText: "SKU-1" });
  await expect(row).toContainText(/1\s500\s₸ → 1\s199\s₸/);
  await expect(row).toContainText(t.reason.undercut_first);
  await expect(row).toContainText(t.result.dry_run);
  await expect(row).toContainText("Магазин А");
  const unchanged = page.getByRole("row").filter({ hasText: "SKU-3" });
  await expect(unchanged).toContainText(fill(t.ui.priceUnchanged, { price: formatPrice(2500) }));
  await expect(unchanged).toContainText(t.reason.out_of_bounds);
  await expect(unchanged).toContainText(t.result.no_change);

  await page.getByRole("button", { name: t.ui.logout, exact: true }).click();
  await expect(page).toHaveURL(/\/login$/);

  expect(fake.csrfMissing).toBe(0);
  expect(fake.unhandled).toBe(0);
  expect(fake.external).toBe(0);
});
