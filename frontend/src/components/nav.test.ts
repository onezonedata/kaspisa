import { describe, expect, it } from "vitest";

import { t } from "@/i18n";
import { NAV_ITEMS, activeNavId } from "./nav";

describe("NAV_ITEMS (Т7)", () => {
  it("пункты и подписи из словаря, в заданном порядке", () => {
    expect(NAV_ITEMS.map((i) => i.id)).toEqual([
      "products",
      "journal",
      "events",
      "notifications",
      "settings",
    ]);
    expect(NAV_ITEMS.map((i) => i.label)).toEqual([
      t.ui.productsLink,
      t.ui.navPriceJournal,
      t.ui.eventsLink,
      t.ui.notificationsLink,
      t.ui.navShopSettings,
    ]);
  });
});

describe("NAV_ITEMS: адреса (Т8)", () => {
  it("при одном магазине — страницы магазина", () => {
    expect(NAV_ITEMS.map((i) => i.href("s1"))).toEqual([
      "/shops/s1/products",
      "/shops/s1/journal",
      "/events",
      "/notifications",
      "/shops/s1",
    ]);
  });

  it("без магазина или при нескольких — список магазинов", () => {
    expect(NAV_ITEMS.map((i) => i.href(null))).toEqual([
      "/shops",
      "/shops",
      "/events",
      "/notifications",
      "/shops",
    ]);
  });
});

describe("activeNavId (Т9)", () => {
  it("определяет пункт по наибольшему совпадению сегментов", () => {
    const at = (path: string) => activeNavId(path, "s1");
    expect(at("/shops/s1/products")).toBe("products");
    expect(at("/shops/s1/products/7")).toBe("products");
    expect(at("/shops/s1/journal")).toBe("journal");
    expect(at("/shops/s1")).toBe("settings");
    expect(at("/events")).toBe("events");
    expect(at("/notifications/")).toBe("notifications");
    expect(at("/shops")).toBeNull();
    expect(at("/shops/s10")).toBeNull();
    expect(at("/login")).toBeNull();
  });

  it("совпадение по сегментам, а не по префиксу строки", () => {
    expect(activeNavId("/shops/s1/productsx", "s1")).toBe("settings");
    expect(activeNavId("/eventsx", "s1")).toBeNull();
    expect(activeNavId("/shops/s1/journal/2026", "s1")).toBe("journal");
    expect(activeNavId("", "s1")).toBeNull();
    expect(activeNavId("/", null)).toBeNull();
  });

  it("пункт другого магазина не подсвечивается", () => {
    expect(activeNavId("/shops/s2/products", "s1")).toBeNull();
  });

  it("без магазина: равенство — первый пункт массива", () => {
    expect(activeNavId("/shops", null)).toBe("products");
    expect(activeNavId("/events", null)).toBe("events");
  });
});
