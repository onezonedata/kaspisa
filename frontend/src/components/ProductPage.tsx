"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback } from "react";

import { getProduct } from "@/api/client";
import { refusalText, t } from "@/i18n";
import { checkNotice, checkTone, countText, lastCheckText, priceText, trackedPosition } from "@/lib/product-view";
import { useLoad } from "@/lib/use-load";
import { ErrorMessage } from "./ErrorMessage";
import { PendingWriteNotice } from "./PendingWriteNotice";
import { PriceChangeList } from "./PriceChangeList";
import { PriceRead } from "./PriceRead";
import { ProductSettingsForm } from "./ProductSettingsForm";
import { RefreshButton } from "./RefreshButton";

export function ProductPage() {
  const { shopId, productId } = useParams<{ shopId: string; productId: string }>();
  const load = useCallback(() => getProduct(shopId, Number(productId)), [shopId, productId]);
  const { data: product, error, loading, reload, setData } = useLoad(load);

  const notice = product === null ? null : checkNotice(product);
  const checkTime = product === null ? null : lastCheckText(product);

  return (
    <main className="page-medium">
      <h1>{product?.title ?? t.ui.productTitle}</h1>
      <nav>
        <Link href={`/shops/${shopId}/products`}>{t.ui.toProducts}</Link>
      </nav>
      <RefreshButton onRefresh={reload} busy={loading} />
      <ErrorMessage error={error} />
      {product !== null ? (
        <>
          <p className="sku">{product.sku}</p>
          <section className="card">
            <h2>{t.ui.productState}</h2>
            <dl className="product-fields">
              <dt>{t.ui.colPrice}</dt>
              <dd className="num" data-field="current_price">
                {priceText(product.current_price)}
              </dd>
              <dt>{t.ui.colPosition}</dt>
              <dd className="num" data-field="position">
                {trackedPosition(product)}
              </dd>
              <dt>{t.ui.colCompetitors}</dt>
              <dd className="num" data-field="competitors_count">
                {countText(product.competitors_count)}
              </dd>
              <dt>{t.ui.colFirstPrice}</dt>
              <dd className="num" data-field="first_place_price">
                {priceText(product.first_place_price)}
              </dd>
              {notice !== null ? (
                <>
                  <dt>{t.ui.colCheck}</dt>
                  <dd data-tone={checkTone(product) ?? undefined}>
                    {notice}
                    {checkTime === null ? null : ` (${checkTime})`}
                  </dd>
                </>
              ) : null}
              {product.refusal_reason !== null ? (
                <>
                  <dt>{t.ui.colRefusal}</dt>
                  <dd data-tone="problem">{refusalText(product.refusal_reason)}</dd>
                </>
              ) : null}
            </dl>
          </section>
          <section className="card">
            <h2>{t.ui.priceReadTitle}</h2>
            <PriceRead
              shopId={shopId}
              product={product}
              onRead={setData}
            />
          </section>
          <PendingWriteNotice pending={product.pending_write} />
          <ProductSettingsForm product={product} onProduct={setData} />
          <section className="card">
            <h2>{t.ui.journalTitle}</h2>
            <PriceChangeList shopId={shopId} productId={product.id} />
          </section>
        </>
      ) : null}
    </main>
  );
}
