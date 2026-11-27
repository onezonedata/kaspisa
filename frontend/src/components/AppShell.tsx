"use client";

import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { ApiError, listShops, logout, logoutAll, me } from "@/api/client";
import type { Shop } from "@/api/types";
import { t } from "@/i18n";
import { useLoad } from "@/lib/use-load";
import { ErrorMessage } from "./ErrorMessage";
import { Icon } from "./icons";
import { ShellShopProvider, ShellUserProvider, type ShellShop } from "./shell-shop";
import { Sidebar, shopMode } from "./Sidebar";
import { SourceFooter } from "./SourceFooter";
import { UpdateBanner } from "./UpdateBanner";

/** Каркас кабинета: проверяет вход через GET /api/auth/me, рисует боковое меню и содержимое. */
export function AppShell({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const { data: user, error, reload } = useLoad(me);
  const [leaving, setLeaving] = useState(false);
  const [leaveError, setLeaveError] = useState<ApiError | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [shops, setShops] = useState<Shop[] | null>(null);
  // Переход на другую страницу закрывает панель (состояние подстраивается во время показа).
  const [shownPath, setShownPath] = useState(pathname);
  if (shownPath !== pathname) {
    setShownPath(pathname);
    setDrawerOpen(false);
  }
  const unauthenticated = error?.status === 401;
  const signedIn = user !== null;

  const openRef = useRef<HTMLButtonElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const restoreFocus = useRef(false);
  const generation = useRef(0);

  useEffect(() => {
    if (unauthenticated) router.replace("/login");
  }, [unauthenticated, router]);

  // Магазины перечитываются при каждой смене адреса: добавление и удаление сопровождаются переходом.
  // Номер поколения отбрасывает поздний ответ устаревшего запроса.
  useEffect(() => {
    if (!signedIn) return;
    const mine = ++generation.current;
    listShops().then(
      (list) => {
        if (generation.current === mine) setShops(Array.isArray(list) ? list : null);
      },
      () => {
        if (generation.current === mine) setShops(null);
      },
    );
    return () => {
      // Ответ этого запроса уже не нужен, даже если новый не начат (размонтирование).
      if (generation.current === mine) generation.current += 1;
    };
  }, [signedIn, pathname]);

  const update = useCallback((shop: Shop) => {
    setShops((prev) => prev?.map((s) => (s.id === shop.id ? shop : s)) ?? prev);
  }, []);
  const shop = shops !== null && shops.length === 1 ? shops[0] : null;
  const shellShop = useMemo<ShellShop>(() => ({ shop, update }), [shop, update]);

  const closeDrawer = useCallback((returnFocus: boolean) => {
    restoreFocus.current = returnFocus;
    setDrawerOpen(false);
  }, []);

  // Фокус: при открытии — на «Закрыть меню», при закрытии пользователем — обратно на «Открыть меню».
  useEffect(() => {
    if (drawerOpen) {
      closeRef.current?.focus();
    } else if (restoreFocus.current) {
      restoreFocus.current = false;
      openRef.current?.focus();
    }
  }, [drawerOpen]);

  useEffect(() => {
    if (!drawerOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeDrawer(true);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [drawerOpen, closeDrawer]);


  // Окно стало шире телефона: панель становится обычным меню, фон не должен остаться inert.
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const query = window.matchMedia("(min-width: 768px)");
    const onChange = (event: { matches: boolean }) => {
      if (event.matches) closeDrawer(false);
    };
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, [closeDrawer]);

  /** На /login — только после 204 или 401: иначе сессия могла остаться жива. */
  async function leave(action: () => Promise<void>) {
    setLeaving(true);
    setLeaveError(null);
    try {
      await action();
    } catch (e) {
      const apiError = e instanceof ApiError ? e : new ApiError(0, "unexpected_error");
      if (apiError.status !== 401) {
        setLeaveError(apiError);
        setLeaving(false);
        return;
      }
    }
    // Полная перезагрузка убирает из DOM скрытые страницы прежнего пользователя.
    // eslint-disable-next-line @next/next/no-location-assign-relative-destination
    window.location.assign("/login");
  }

  if (user === null) {
    if (error !== null && !unauthenticated) {
      return (
        <div>
          <ErrorMessage error={error} />
          <button type="button" onClick={reload}>
            {t.ui.retry}
          </button>
        </div>
      );
    }
    return <p className="loading">{t.ui.loading}</p>;
  }

  const mode = shop ? shopMode(shop) : null;

  return (
    <div className="shell" data-open={drawerOpen}>
      <header className="topbar" inert={drawerOpen}>
        <button
          type="button"
          ref={openRef}
          className="topbar-btn"
          aria-label={t.ui.menuOpen}
          aria-expanded={drawerOpen}
          aria-controls="app-sidebar"
          onClick={() => setDrawerOpen(true)}
        >
          <Icon name="menu" />
        </button>
        <span className="side-brand">{t.ui.appName}</span>
        {mode ? (
          <span className="topbar-mode" data-mode={mode.mode} role="img" aria-label={mode.text}>
            <span className="mode-dot" aria-hidden="true" />
          </span>
        ) : null}
      </header>
      <div
        className="backdrop"
        aria-hidden="true"
        hidden={!drawerOpen}
        onClick={() => closeDrawer(true)}
      />
      <aside id="app-sidebar" className="sidebar">
        <Sidebar
          email={user.email}
          isAdmin={user.is_admin}
          shop={shop}
          pathname={pathname}
          leaving={leaving}
          leaveError={leaveError}
          onLeave={() => void leave(logout)}
          onLeaveAll={() => void leave(logoutAll)}
          closeRef={closeRef}
          onClose={() => closeDrawer(true)}
          onNavigate={() => closeDrawer(false)}
        />
      </aside>
      <div className="shell-content" inert={drawerOpen}>
        <UpdateBanner />
        <ShellUserProvider isAdmin={user.is_admin}>
          <ShellShopProvider value={shellShop}>{children}</ShellShopProvider>
        </ShellUserProvider>
        <SourceFooter />
      </div>
    </div>
  );
}
