"use client";

import { useCallback, useEffect, useState, type Dispatch, type SetStateAction } from "react";

import { ApiError } from "@/api/client";

export interface Loaded<T> {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
  reload: () => void;
  setData: Dispatch<SetStateAction<T | null>>;
}

/**
 * Загружает данные при показе компонента. Признака «уже загружено» нет:
 * при возврате на страницу (Activity) эффект запускается снова и данные перечитываются.
 */
export function useLoad<T>(load: () => Promise<T>): Loaded<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [version, setVersion] = useState(0);
  // Для какого load и какой версии данные получены: расхождение — идёт загрузка.
  const [settled, setSettled] = useState<{ load: () => Promise<T>; version: number } | null>(null);

  useEffect(() => {
    let cancelled = false;
    load().then(
      (result) => {
        if (cancelled) return;
        setData(result);
        setError(null);
        setSettled({ load, version });
      },
      (e: unknown) => {
        if (cancelled) return;
        setError(e instanceof ApiError ? e : new ApiError(0, "unexpected_error"));
        setSettled({ load, version });
      },
    );
    return () => {
      cancelled = true;
    };
  }, [load, version]);

  const reload = useCallback(() => setVersion((v) => v + 1), []);

  const loading = settled === null || settled.load !== load || settled.version !== version;
  // После смены load старые данные и ошибка не показываются: они относятся к другому запросу.
  const sameLoad = settled !== null && settled.load === load;
  return {
    data: sameLoad ? data : null,
    error: sameLoad ? error : null,
    loading,
    reload,
    setData,
  };
}
