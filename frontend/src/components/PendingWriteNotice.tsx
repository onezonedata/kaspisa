import type { PendingWrite } from "@/api/types";
import { codeText, fill, t } from "@/i18n";
import { formatDateTime, formatPrice } from "@/lib/format";

/** Цены записи: одна, если у всех городов равны, иначе «город: цена». */
function pricesText(prices: PendingWrite["prices"]): string {
  const entries = Object.entries(prices);
  if (entries.length > 0 && entries.every(([, price]) => price === entries[0][1])) {
    return formatPrice(entries[0][1]);
  }
  return entries.map(([city, price]) => `${city}: ${formatPrice(price)}`).join(", ");
}

export function PendingWriteNotice({ pending }: { pending: PendingWrite | null }) {
  if (pending === null) return null;
  return (
    <section className="card">
      <h2>{t.ui.pendingTitle}</h2>
      <p>{fill(t.ui.pendingSent, { prices: pricesText(pending.prices) })}</p>
      <p>
        {t.ui.pendingStatus}: {codeText(t.pendingStatus, pending.status)}; {t.ui.pendingSentAt}{" "}
        {formatDateTime(pending.sent_at)}; {t.ui.pendingHoldUntil}{" "}
        {formatDateTime(pending.hold_until)}
      </p>
      {pending.outside_bounds ? <p role="alert" className="error">{t.ui.pendingOutsideBounds}</p> : null}
    </section>
  );
}
