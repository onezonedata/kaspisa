"use client";

import Link from "next/link";
import { useState, type Ref } from "react";

import type { Shop } from "@/api/types";
import { t } from "@/i18n";
import { applySidebar, isSidebarCollapsed } from "@/lib/prefs";
import { ErrorMessage } from "./ErrorMessage";
import { Icon } from "./icons";
import { NAV_ITEMS, activeNavId } from "./nav";
import { ThemeToggle } from "./ThemeToggle";
import type { ApiError } from "@/api/client";

/** Режим бота для меню: остановка важнее выбранного режима. */
export function shopMode(shop: Shop): { mode: "off" | "dry_run" | "live" | "halted"; text: string } {
  return shop.halt_reason !== null
    ? { mode: "halted", text: t.ui.botHalted }
    : { mode: shop.bot_mode, text: t.botMode[shop.bot_mode] };
}

interface SidebarProps {
  email: string;
  shop: Shop | null;
  pathname: string;
  leaving: boolean;
  leaveError: ApiError | null;
  onLeave: () => void;
  onLeaveAll: () => void;
  /** Кнопка «Закрыть меню» (только на телефоне): ссылка для переноса фокуса. */
  closeRef: Ref<HTMLButtonElement>;
  onClose: () => void;
  /** Нажатие на пункт: панель на телефоне закрывается и для ссылки на текущую страницу. */
  onNavigate: () => void;
}

/** Боковое меню: название, магазин с режимом бота, разделы, тема, выход. Один экземпляр в DOM. */
export function Sidebar({
  email,
  shop,
  pathname,
  leaving,
  leaveError,
  onLeave,
  onLeaveAll,
  closeRef,
  onClose,
  onNavigate,
}: SidebarProps) {
  // Начальное значение — из атрибута <html>, который ставит встроенный скрипт до отрисовки.
  const [collapsed, setCollapsed] = useState(isSidebarCollapsed);
  const shopId = shop?.id ?? null;
  const active = activeNavId(pathname, shopId);
  const state = shop ? shopMode(shop) : null;

  function toggleCollapsed() {
    applySidebar(!collapsed);
    setCollapsed(!collapsed);
  }

  return (
    <>
      <div className="side-head">
        <span className="side-brand">{t.ui.appName}</span>
        <button type="button" className="side-btn side-close" ref={closeRef} onClick={onClose}>
          <Icon name="close" />
          <span className="nav-label">{t.ui.menuClose}</span>
        </button>
      </div>
      {shop && state ? (
        <div className="side-shop" data-mode={state.mode}>
          <span className="side-shop-title">{t.ui.shopTitle}</span>
          <span className="side-shop-uid">{shop.merchant_uid ?? "—"}</span>
          <span className="side-shop-mode">
            <span className="mode-dot" aria-hidden="true" />
            <span className="mode-text">{state.text}</span>
          </span>
        </div>
      ) : null}
      <nav aria-label={t.ui.navLabel}>
        <ul className="side-nav">
          {NAV_ITEMS.map((item) => (
            <li key={item.id}>
              <Link
                href={item.href(shopId)}
                aria-current={active === item.id ? "page" : undefined}
                onClick={onNavigate}
              >
                <Icon name={item.icon} />
                <span className="nav-label">{item.label}</span>
              </Link>
            </li>
          ))}
        </ul>
      </nav>
      <div className="side-foot">
        <span className="side-email">{email}</span>
        <ThemeToggle />
        <button
          type="button"
          className="side-btn side-collapse"
          aria-pressed={collapsed}
          onClick={toggleCollapsed}
        >
          <Icon name="chevron" />
          <span className="nav-label">{t.ui.sidebarCollapse}</span>
        </button>
        <button type="button" className="side-btn" disabled={leaving} onClick={onLeave}>
          <Icon name="logout" />
          <span className="nav-label">{t.ui.logout}</span>
        </button>
        <button type="button" className="side-btn side-plain" disabled={leaving} onClick={onLeaveAll}>
          <Icon name="logout" />
          <span className="nav-label">{t.ui.logoutAll}</span>
        </button>
        <ErrorMessage error={leaveError} />
      </div>
    </>
  );
}
