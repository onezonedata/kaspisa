"use client";

import Link from "next/link";
import { useParams } from "next/navigation";

import { t } from "@/i18n";
import { PriceChangeList } from "./PriceChangeList";

export function JournalPage() {
  const { shopId } = useParams<{ shopId: string }>();
  return (
    <main className="page">
      <h1>{t.ui.journalTitle}</h1>
      <nav>
        <Link href={`/shops/${shopId}`}>{t.ui.toShop}</Link>
      </nav>
      <PriceChangeList shopId={shopId} />
    </main>
  );
}
