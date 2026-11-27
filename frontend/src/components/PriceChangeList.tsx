"use client";

import Link from "next/link";
import { useCallback, useState } from "react";

import { listPriceChanges } from "@/api/client";
import type { PriceChange } from "@/api/types";
import { codeText, fill, journalErrorText, t } from "@/i18n";
import { formatDateTime, formatPrice } from "@/lib/format";
import { useLoad } from "@/lib/use-load";
import { ErrorMessage } from "./ErrorMessage";
import { RefreshButton } from "./RefreshButton";

const LIMIT = 50;

/** Подробности решения: только то, что есть в строке. */
function details(c: PriceChange): string[] {
  const out: string[] = [];
  if (c.competitor_name !== null && c.competitor_price !== null) {
    out.push(
      fill(t.ui.competitorDetail, {
        name: c.competitor_name,
        id: c.competitor_merchant_id ?? "—",
        price: formatPrice(c.competitor_price),
      }),
    );
  }
  if (c.target_price !== null) out.push(fill(t.ui.targetDetail, { price: formatPrice(c.target_price) }));
  if (c.capped_at_max) out.push(t.ui.cappedAtMax);
  if (c.skipped_competitors > 0) out.push(fill(t.ui.skippedCompetitors, { n: c.skipped_competitors }));
  if (c.cities_aligned) out.push(t.ui.citiesAligned);
  return out;
}

/** Журнал изменений цен магазина или одного товара (если задан `productId`). */
export function PriceChangeList({ shopId, productId }: { shopId: string; productId?: number }) {
  const [offset, setOffset] = useState(0);
  const load = useCallback(
    () => listPriceChanges(shopId, { productId, limit: LIMIT, offset }),
    [shopId, productId, offset],
  );
  const { data, error, loading, reload } = useLoad(load);
  const items = data?.items ?? [];

  return (
    <section className="stack">
      <RefreshButton onRefresh={reload} busy={loading} />
      <ErrorMessage error={error} />
      {data === null && loading ? <p>{t.ui.loading}</p> : null}
      {data !== null && items.length === 0 && offset === 0 ? <p>{t.ui.journalEmpty}</p> : null}
      {items.length > 0 ? (
        <div className="table-scroll">
<table>
          <thead>
            <tr>
              <th>{t.ui.colWhen}</th>
              {productId === undefined ? <th>{t.ui.colProduct}</th> : null}
              <th className="num">{t.ui.colChange}</th>
              <th>{t.ui.colReason}</th>
              <th>{t.ui.colResult}</th>
              <th>{t.ui.colError}</th>
            </tr>
          </thead>
          <tbody>
            {items.map((c) => (
              <tr key={c.id}>
                <td>{formatDateTime(c.created_at)}</td>
                {productId === undefined ? (
                  <td className="wrap">
                    {c.sku}{" "}
                    <Link href={`/shops/${shopId}/products/${c.product_id}`}>{c.title}</Link>
                  </td>
                ) : null}
                <td className="num">
                  {c.result === "no_change"
                    ? fill(t.ui.priceUnchanged, { price: formatPrice(c.new_price) })
                    : `${formatPrice(c.old_price)} → ${formatPrice(c.new_price)}`}
                </td>
                <td className="wrap">
                  <span>{codeText(t.reason, c.reason)}</span>
                  {details(c).map((d) => (
                    <div key={d}>{d}</div>
                  ))}
                </td>
                <td>{codeText(t.result, c.result)}</td>
                <td className="wrap">{c.error === null ? "" : journalErrorText(c.error)}</td>
              </tr>
            ))}
          </tbody>
        </table>
</div>
      ) : null}
      {data !== null ? (
        <div className="pager">
          <button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - LIMIT))}>
            {t.ui.pagePrev}
          </button>
          <button type="button" disabled={items.length < LIMIT} onClick={() => setOffset(offset + LIMIT)}>
            {t.ui.pageNext}
          </button>
        </div>
      ) : null}
    </section>
  );
}
