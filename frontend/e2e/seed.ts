// Данные заглушки для сквозных тестов вида и снимков: выдуманные, без настоящих магазинов и секретов.
import type { Product } from "../src/api/types";
import { makePriceChange, makeProduct, makeShop } from "../src/test/fixtures";
import type { FakeApi } from "./fake-api";

export const DEMO_EMAIL = "seller@example.kz";
export const DEMO_PASSWORD = "test-password-1";
const SHOP_ID = "00000000-0000-4000-8000-000000000100";

/** Заголовок на казахском: кириллица со знаками расширенного набора проверяется на глаз. */
export const KAZAKH_TITLE = "Қазақша тауар, Әә Ғғ Үү Ұұ Іі";

const TITLES = [
  "Кабель USB-C, 1 м",
  "Чехол для телефона",
  "Наушники беспроводные",
  KAZAKH_TITLE,
  "Зарядное устройство 20 Вт",
  "Подставка для ноутбука",
  "Мышь компьютерная",
  "Клавиатура механическая",
  "Лампа настольная",
  "Рюкзак городской",
  "Термос стальной 0,5 л",
  "Коврик для йоги",
];

const CHECKED = "2026-11-15T10:00:00Z";

/** Товары заглушки: каждый тон колонки «Проверка», отказ и неотслеживаемые. */
export function demoProducts(count = 12): Product[] {
  const base = (i: number): Partial<Product> => ({
    id: i + 1,
    shop_id: SHOP_ID,
    sku: `DEMO-${String(i + 1).padStart(3, "0")}`,
    master_sku: `DEMO-${String(i + 1).padStart(3, "0")}`,
    title: TITLES[i % TITLES.length] + (i >= TITLES.length ? ` ${i + 1}` : ""),
    tracked: true,
    min_price: 900 + i * 10,
    max_price: 2500 + i * 10,
    step: 5,
    current_price: 1500 + i * 25,
    competitors_count: 4,
    first_place_price: 1400 + i * 25,
    last_checked_at: CHECKED,
    decision_reason: "undercut_first",
    decision_target_price: 1399 + i * 25,
    position: 2,
  });
  const variants: Partial<Product>[] = [
    { position: 1, first_place_price: 1500, current_price: 1500 },
    { position: 2 },
    {
      decision_reason: null,
      decision_target_price: null,
      last_checked_at: null,
      position: null,
      competitors_count: null,
    },
    {
      min_price: null,
      max_price: null,
      decision_reason: "bounds_missing",
      decision_target_price: null,
    },
    { skip_reason: "kaspi_error" },
    { decision_write: "refused", refusal_reason: "stock_counted" },
    {
      tracked: false,
      min_price: null,
      max_price: null,
      decision_reason: null,
      decision_target_price: null,
      position: null,
    },
    { position: null, position_over: 20, competitors_count: 20 },
    { auto_raise: true, position: 1 },
  ];
  return Array.from({ length: count }, (_, i) =>
    makeProduct({ ...base(i), ...variants[i % variants.length] }),
  );
}

/** Кабинет без входа через форму: пользователь есть, магазин подключён, товары и журнал заполнены. */
export function seedCabinet(fake: FakeApi, productCount = 12): string {
  fake.user = { id: "00000000-0000-4000-8000-000000000001", email: DEMO_EMAIL, is_admin: true };
  fake.account = { email: DEMO_EMAIL, password: DEMO_PASSWORD };
  fake.shops = [
    makeShop({
      id: SHOP_ID,
      merchant_uid: "30000001",
      connection_state: "connected",
      bot_mode: "dry_run",
      city_id: "710000000",
      catalog_synced_at: CHECKED,
    }),
  ];
  fake.products = demoProducts(productCount);
  fake.priceChanges = [1, 2, 3].map((id) =>
    makePriceChange({
      id,
      product_id: id,
      sku: `DEMO-00${id}`,
      title: TITLES[id - 1],
      old_price: 1500 + id,
      new_price: 1199 + id,
      competitor_name: "Магазин А",
      competitor_price: 1200 + id,
    }),
  );
  return SHOP_ID;
}
