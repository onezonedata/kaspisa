import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  PREFS_SCRIPT,
  SIDEBAR_KEY,
  THEME_KEY,
  applySidebar,
  applyTheme,
  currentTheme,
  isSidebarCollapsed,
  readPref,
  writePref,
} from "./prefs";

const root = () => document.documentElement;

function runScript() {
  new Function(PREFS_SCRIPT)();
}

function stubMatchMedia(dark: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => ({ matches: dark && query.includes("dark"), media: query })),
  );
}

beforeEach(() => {
  localStorage.clear();
  delete root().dataset.theme;
  delete root().dataset.sidebar;
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("встроенный скрипт (Т3)", () => {
  it("тёмная и светлая темы из хранилища", () => {
    localStorage.setItem(THEME_KEY, "dark");
    runScript();
    expect(root().dataset.theme).toBe("dark");

    localStorage.setItem(THEME_KEY, "light");
    runScript();
    expect(root().dataset.theme).toBe("light");
  });

  it("посторонние значения и пустое хранилище атрибут не ставят", () => {
    localStorage.setItem(THEME_KEY, 'x"><b>');
    runScript();
    expect(root().dataset.theme).toBeUndefined();

    localStorage.clear();
    runScript();
    expect(root().dataset.theme).toBeUndefined();
    expect(root().dataset.sidebar).toBeUndefined();
  });

  it("недоступное хранилище — без исключения и без атрибутов", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("denied");
    });
    expect(runScript).not.toThrow();
    expect(root().dataset.theme).toBeUndefined();
    expect(root().dataset.sidebar).toBeUndefined();
  });

  it("свёрнутое меню — только значение collapsed", () => {
    localStorage.setItem(SIDEBAR_KEY, "collapsed");
    runScript();
    expect(root().dataset.sidebar).toBe("collapsed");

    delete root().dataset.sidebar;
    localStorage.setItem(SIDEBAR_KEY, "expanded");
    runScript();
    expect(root().dataset.sidebar).toBeUndefined();
  });
});

describe("тема и меню (Т4)", () => {
  it("currentTheme: атрибут сильнее системной темы", () => {
    stubMatchMedia(false);
    root().dataset.theme = "dark";
    expect(currentTheme()).toBe("dark");
  });

  it("currentTheme: без атрибута берёт системную тему", () => {
    stubMatchMedia(true);
    expect(currentTheme()).toBe("dark");
    stubMatchMedia(false);
    expect(currentTheme()).toBe("light");
  });

  it("currentTheme: без matchMedia — светлая", () => {
    expect(currentTheme()).toBe("light");
  });

  it("applyTheme пишет атрибут и хранилище", () => {
    applyTheme("dark");
    expect(root().dataset.theme).toBe("dark");
    expect(localStorage.getItem(THEME_KEY)).toBe("dark");
  });

  it("applyTheme при ошибке записи: тема действует, исключения нет", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    expect(() => applyTheme("dark")).not.toThrow();
    expect(root().dataset.theme).toBe("dark");
    expect(writePref(THEME_KEY, "dark")).toBe(false);
  });

  it("readPref при ошибке хранилища возвращает null", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("denied");
    });
    expect(readPref(THEME_KEY)).toBeNull();
  });

  it("applySidebar: атрибут и хранилище, обратно — атрибут удаляется", () => {
    applySidebar(true);
    expect(root().dataset.sidebar).toBe("collapsed");
    expect(localStorage.getItem(SIDEBAR_KEY)).toBe("collapsed");
    expect(isSidebarCollapsed()).toBe(true);

    applySidebar(false);
    expect(root().dataset.sidebar).toBeUndefined();
    expect(localStorage.getItem(SIDEBAR_KEY)).toBe("expanded");
    expect(isSidebarCollapsed()).toBe(false);
  });

  it("applySidebar при ошибке записи всё равно меняет атрибут", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    expect(() => applySidebar(true)).not.toThrow();
    expect(root().dataset.sidebar).toBe("collapsed");
  });
});
