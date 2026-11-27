import Link from "next/link";

import { SourceFooter } from "@/components/SourceFooter";
import { t } from "@/i18n";

/** Страница 404: корневой макет подвала не выводит, ссылка на исходный код нужна и здесь. */
export default function NotFound() {
  return (
    <>
      <main className="auth">
        <h1>{t.ui.notFoundTitle}</h1>
        <p>
          <Link href="/shops">{t.ui.notFoundBack}</Link>
        </p>
      </main>
      <SourceFooter />
    </>
  );
}
