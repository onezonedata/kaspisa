"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { me } from "@/api/client";
import { t } from "@/i18n";

/** Главная: переход в кабинет или на вход. */
export default function Home() {
  const router = useRouter();

  useEffect(() => {
    let cancelled = false;
    me().then(
      () => !cancelled && router.replace("/shops"),
      () => !cancelled && router.replace("/login"),
    );
    return () => {
      cancelled = true;
    };
  }, [router]);

  return <p className="loading">{t.ui.loading}</p>;
}
