"use client";

import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { useCallback, useState } from "react";

import { getCatalogSummary } from "@/api/client";
import type { Shop } from "@/api/types";
import { fill, t } from "@/i18n";
import { useLoad } from "@/lib/use-load";
import { CatalogSync } from "./CatalogSync";
import { ProductsTable } from "./ProductsTable";
import { useShellShop } from "./shell-shop";

export function ProductsPage() {
  const { shopId } = useParams<{ shopId: string }>();
  // Начальный фильтр берётся из адреса (?tracked=true|false); обратно в адрес он не пишется.
  const tracked = useSearchParams().get("tracked");
  const initialTracked = tracked === "true" || tracked === "false" ? tracked : "";
  const { shop, update } = useShellShop();
  const ownShop = shop !== null && shop.id === shopId ? shop : null;
  // Номер загрузки каталога: новый номер перечитывает список и счётчик.
  const [syncVersion, setSyncVersion] = useState(0);
  const onSynced = (fresh: Shop) => {
    update(fresh);
    setSyncVersion((v) => v + 1);
  };

  // Счётчик вспомогательный: ошибка сводки не показывается, связь видна по таблице.
  // syncVersion — в зависимостях: после загрузки каталога сводка запрашивается заново.
  const loadSummary = useCallback(
    () => getCatalogSummary(shopId),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [shopId, syncVersion],
  );
  const { data: summary } = useLoad(loadSummary);
  const counter =
    summary === null
      ? null
      : summary.total !== null
        ? fill(t.ui.productsCount, { total: summary.total, tracked: summary.tracked ?? 0 })
        : fill(t.ui.trackedCount, { n: summary.tracked ?? 0 });

  return (
    <main>
      <div className="page-head">
        <h1>{t.ui.productsTitle}</h1>
        {counter === null ? null : <span className="count">{counter}</span>}
        {ownShop === null ? null : <CatalogSync shop={ownShop} onShop={onSynced} />}
      </div>
      <nav>
        <Link href={`/shops/${shopId}`}>{t.ui.toShop}</Link>
      </nav>
      <ProductsTable shopId={shopId} initialTracked={initialTracked} reloadKey={syncVersion} />
    </main>
  );
}
