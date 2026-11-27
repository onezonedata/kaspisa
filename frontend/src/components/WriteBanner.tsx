import type { Shop } from "@/api/types";
import { t } from "@/i18n";

/**
 * Что обработчик сообщил о записи цен (ADR 0024): `true` — запись запрещена на сервере,
 * `null` — обработчик не отвечает, `false` — баннера нет.
 */
export function WriteBanner({ shop }: { shop: Shop }) {
  if (shop.server_dry_run === false) return null;
  return (
    <section className="banner tone-warn">
      <p role="status">
        {shop.server_dry_run === true ? t.ui.bannerServerDryRun : t.ui.bannerWorkerSilent}
      </p>
    </section>
  );
}
