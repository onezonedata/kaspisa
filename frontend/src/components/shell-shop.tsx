"use client";

import { createContext, useContext, type ReactNode } from "react";

import type { Shop } from "@/api/types";

export interface ShellShop {
  /** Единственный магазин пользователя; `null`, если магазинов нет, их больше одного или список не загрузился. */
  shop: Shop | null;
  /** Заменяет магазин каркаса свежим, только если `id` совпадает. */
  update: (shop: Shop) => void;
}

const ShellShopContext = createContext<ShellShop>({ shop: null, update: () => {} });

export function ShellShopProvider({ value, children }: { value: ShellShop; children: ReactNode }) {
  return <ShellShopContext value={value}>{children}</ShellShopContext>;
}

export const useShellShop = () => useContext(ShellShopContext);
