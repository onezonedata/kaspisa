"use client";

import { useState } from "react";

import { t } from "@/i18n";
import { applyTheme, currentTheme } from "@/lib/prefs";
import { Icon } from "./icons";

/** Две позиции: тёмная тема включена или нет. До первого нажатия тема — как в системе. */
export function ThemeToggle() {
  const [theme, setTheme] = useState(currentTheme);

  function toggle() {
    const next = theme === "dark" ? "light" : "dark";
    applyTheme(next);
    setTheme(next);
  }

  return (
    <button type="button" className="side-btn" aria-pressed={theme === "dark"} onClick={toggle}>
      <Icon name="moon" />
      <span className="nav-label">{t.ui.themeDark}</span>
    </button>
  );
}
