// Пункты бокового меню: новая страница кабинета добавляется одной строкой массива (ADR 0035).
import { t } from "@/i18n";
import type { IconName } from "./icons";

export type NavId = "products" | "journal" | "events" | "notifications" | "settings";

export interface NavItem {
  id: NavId;
  label: string;
  icon: IconName;
  /** Адрес пункта; `null` вместо магазина — его нет или их больше одного. */
  href: (shopId: string | null) => string;
}

export const NAV_ITEMS: readonly NavItem[] = [
  {
    id: "products",
    label: t.ui.productsLink,
    icon: "box",
    href: (s) => (s ? `/shops/${s}/products` : "/shops"),
  },
  {
    id: "journal",
    label: t.ui.navPriceJournal,
    icon: "history",
    href: (s) => (s ? `/shops/${s}/journal` : "/shops"),
  },
  { id: "events", label: t.ui.eventsLink, icon: "shield", href: () => "/events" },
  { id: "notifications", label: t.ui.notificationsLink, icon: "bell", href: () => "/notifications" },
  {
    id: "settings",
    label: t.ui.navShopSettings,
    icon: "settings",
    href: (s) => (s ? `/shops/${s}` : "/shops"),
  },
];

const segments = (path: string) => path.split("/").filter((s) => s !== "");

/**
 * Пункт текущей страницы: адрес пункта — начало пути по сегментам, выигрывает самый длинный;
 * при равенстве — первый в массиве.
 */
export function activeNavId(
  pathname: string,
  shopId: string | null,
  items: readonly NavItem[] = NAV_ITEMS,
): NavId | null {
  const path = segments(pathname);
  let best: { id: NavId; length: number } | null = null;
  for (const item of items) {
    const own = segments(item.href(shopId));
    const matches = own.length <= path.length && own.every((s, i) => path[i] === s);
    if (matches && (best === null || own.length > best.length)) {
      best = { id: item.id, length: own.length };
    }
  }
  return best?.id ?? null;
}
