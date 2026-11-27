import Link from "next/link";

import type { Shop } from "@/api/types";
import { codeText, t } from "@/i18n";
import { formatDateTime } from "@/lib/format";

export function ShopList({ shops }: { shops: Shop[] }) {
  if (shops.length === 0) return <p>{t.ui.shopsEmpty}</p>;
  return (
    <ul className="shop-list card">
      {shops.map((shop) => (
        <li key={shop.id}>
          <Link href={`/shops/${shop.id}`}>
            {shop.merchant_uid ?? `${t.ui.shopNoConnection} (${formatDateTime(shop.created_at)})`}
          </Link>{" "}
          <span>{codeText(t.connectionState, shop.connection_state)}</span>
          {shop.halt_reason !== null ? <strong className="warn"> {t.ui.botHalted}</strong> : null}
        </li>
      ))}
    </ul>
  );
}
