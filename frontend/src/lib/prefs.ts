// Настройки вида в браузере: тема и состояние бокового меню (ADR 0035).
// Хранилище может быть недоступно (приватный режим, запрет cookie): тогда выбор действует до перезагрузки.

export type Theme = "light" | "dark";

export const THEME_KEY = "kaspisa.theme";
export const SIDEBAR_KEY = "kaspisa.sidebar"; // "collapsed" | "expanded"

/**
 * Встроенный скрипт для `<head>`: выполняется до первой отрисовки и ставит `data-theme` и
 * `data-sidebar` на `<html>`. Допускает только известные значения; любая ошибка хранилища
 * гасится.
 */
export const PREFS_SCRIPT =
  "(function(){try{" +
  "var d=document.documentElement;" +
  `var t=localStorage.getItem(${JSON.stringify(THEME_KEY)});` +
  'if(t==="dark"||t==="light"){d.dataset.theme=t;}' +
  `if(localStorage.getItem(${JSON.stringify(SIDEBAR_KEY)})==="collapsed"){d.dataset.sidebar="collapsed";}` +
  "}catch(e){}})();";

/** Читает значение; любая ошибка хранилища даёт `null`. */
export function readPref(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

/** Пишет значение; `false`, если хранилище недоступно. */
export function writePref(key: string, value: string): boolean {
  try {
    localStorage.setItem(key, value);
    return true;
  } catch {
    return false;
  }
}

/** Действующая тема: атрибут, иначе системная, иначе светлая. */
export function currentTheme(): Theme {
  if (typeof document === "undefined") return "light";
  const attr = document.documentElement.dataset.theme;
  if (attr === "dark" || attr === "light") return attr;
  if (typeof window.matchMedia === "function") {
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  return "light";
}

/** Ставит тему сразу и пытается её запомнить. */
export function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme;
  writePref(THEME_KEY, theme);
}

export function isSidebarCollapsed(): boolean {
  if (typeof document === "undefined") return false;
  return document.documentElement.dataset.sidebar === "collapsed";
}

/** Сворачивает или разворачивает меню сразу и пытается запомнить выбор. */
export function applySidebar(collapsed: boolean): void {
  if (collapsed) {
    document.documentElement.dataset.sidebar = "collapsed";
  } else {
    delete document.documentElement.dataset.sidebar;
  }
  writePref(SIDEBAR_KEY, collapsed ? "collapsed" : "expanded");
}
