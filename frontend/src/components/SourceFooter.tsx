import { t } from "@/i18n";
import { APP_VERSION, SOURCE_HREF } from "@/lib/source";

/** Подвал корневого макета: ссылка на исходный код и версия. Серверный, статичный. */
export function SourceFooter() {
  return (
    <footer className="source-footer">
      <a href={SOURCE_HREF} target="_blank" rel="noopener noreferrer">
        {t.ui.sourceCode}
      </a>
      {" · "}
      {t.ui.versionLabel} {APP_VERSION}
    </footer>
  );
}
