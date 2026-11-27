"use client";

import Link from "next/link";
import { useCallback } from "react";

import { getCatalogSummary } from "@/api/client";
import type { Shop } from "@/api/types";
import { fill, t } from "@/i18n";
import { formatDateTime } from "@/lib/format";
import { useLoad } from "@/lib/use-load";
import { ErrorMessage } from "./ErrorMessage";

function Totals({ shop, syncedAt }: { shop: Shop; syncedAt: string }) {
  // Новый объект магазина (открытие, «Обновить», загрузка каталога) перечитывает сводку.
  const load = useCallback(() => getCatalogSummary(shop.id), [shop]);
  const { data, error } = useLoad(load);
  const time = formatDateTime(syncedAt);
  return (
    <>
      <ErrorMessage error={error} />
      {data !== null && data.total === null ? (
        <p>{fill(t.ui.catalogTooLarge, { time })}</p>
      ) : null}
      {data !== null && data.total !== null ? (
        <>
          <p>{fill(t.ui.catalogSummary, { total: data.total, time })}</p>
          <p>
            <Link href={`/shops/${shop.id}/products?tracked=true`}>
              {fill(t.ui.trackedCount, { n: data.tracked ?? 0 })}
            </Link>
          </p>
        </>
      ) : null}
    </>
  );
}

/** Блок «Каталог» страницы магазина: число товаров, время загрузки, отслеживаемые. */
export function CatalogSummary({ shop }: { shop: Shop }) {
  return (
    <section className="catalog-summary">
      {shop.catalog_loading ? (
        <p role="status">{t.ui.catalogLoading}</p>
      ) : shop.catalog_synced_at === null ? (
        <p>{t.ui.catalogNotLoaded}</p>
      ) : (
        <Totals shop={shop} syncedAt={shop.catalog_synced_at} />
      )}
    </section>
  );
}
