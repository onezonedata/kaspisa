"use client";

import { getVersion } from "@/api/client";
import { fill, t } from "@/i18n";
import { useLoad } from "@/lib/use-load";

const RELEASE_PREFIX = "https://github.com/";

/** Плашка «вышла новая версия»; при ошибке запроса и без обновления ничего не показывает. */
export function UpdateBanner() {
  const { data } = useLoad(getVersion);
  if (!data || !data.update_available || data.latest === null) return null;

  const url = data.release_url;
  const safeUrl = url !== null && url.startsWith(RELEASE_PREFIX) ? url : null;
  return (
    <section className="banner tone-info">
      <p role="status">{fill(t.ui.updateAvailable, { latest: data.latest, current: data.current })}</p>
      {safeUrl !== null ? (
        <a href={safeUrl} target="_blank" rel="noopener noreferrer">
          {t.ui.updateHow}
        </a>
      ) : null}
    </section>
  );
}
