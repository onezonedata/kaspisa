"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect } from "react";

import { getShop } from "@/api/client";
import { t } from "@/i18n";
import { useLoad } from "@/lib/use-load";
import { CatalogSummary } from "./CatalogSummary";
import { CatalogSync } from "./CatalogSync";
import { ConnectionPanel } from "./ConnectionPanel";
import { CredentialsForm } from "./CredentialsForm";
import { DeleteShopButton } from "./DeleteShopButton";
import { ErrorMessage } from "./ErrorMessage";
import { HaltBanner } from "./HaltBanner";
import { RefreshButton } from "./RefreshButton";
import { ShopSettingsForm } from "./ShopSettingsForm";
import { useShellShop } from "./shell-shop";
import { WriteBanner } from "./WriteBanner";

export function ShopPage() {
  const { shopId } = useParams<{ shopId: string }>();
  const load = useCallback(() => getShop(shopId), [shopId]);
  const { data: shop, error, loading, reload, setData } = useLoad(load);
  const { shop: menuShop, update } = useShellShop();
  // Меню ведёт к товарам и журналу только своего (единственного) магазина.
  const showLinks = menuShop?.id !== shopId;

  // Блок магазина в меню сразу показывает свежий режим бота и остановку.
  useEffect(() => {
    if (shop) update(shop);
  }, [shop, update]);

  return (
    <main className="page-narrow">
      <h1>{shop?.merchant_uid ?? t.ui.shopTitle}</h1>
      {showLinks ? (
        <p>
          <Link href={`/shops/${shopId}/products`}>{t.ui.toProducts}</Link>{" "}
          <Link href={`/shops/${shopId}/journal`}>{t.ui.toJournal}</Link>
        </p>
      ) : null}
      <RefreshButton onRefresh={reload} busy={loading} />
      <ErrorMessage error={error} />
      {shop === null && loading ? <p>{t.ui.loading}</p> : null}
      {shop !== null ? (
        <>
          <WriteBanner shop={shop} />
          <HaltBanner shop={shop} onShop={setData} />
          <ConnectionPanel shop={shop} onShop={setData} />
          <div className={shop.connection_state === "connected" ? "card" : undefined}>
            {shop.connection_state === "connected" ? <CatalogSummary shop={shop} /> : null}
            <CatalogSync shop={shop} onShop={setData} />
          </div>
          <ShopSettingsForm shop={shop} onShop={setData} />
          <CredentialsForm shop={shop} onShop={setData} />
          <DeleteShopButton shopId={shop.id} />
        </>
      ) : null}
    </main>
  );
}
