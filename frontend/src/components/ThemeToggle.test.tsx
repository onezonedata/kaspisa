import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { t } from "@/i18n";
import { ThemeToggle } from "./ThemeToggle";

function reset() {
  delete document.documentElement.dataset.theme;
  localStorage.clear();
}

beforeEach(reset);
afterEach(() => {
  reset();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const toggle = () => screen.getByRole("button", { name: t.ui.themeDark });

describe("ThemeToggle (Т14)", () => {
  it("светлая тема системы: не нажата; нажатие включает тёмную и запоминает", () => {
    vi.stubGlobal("matchMedia", () => ({ matches: false }));
    render(<ThemeToggle />);
    expect(toggle()).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(toggle());
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem("kaspisa.theme")).toBe("dark");
    expect(toggle()).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(toggle());
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(toggle()).toHaveAttribute("aria-pressed", "false");
  });

  it("тёмная тема уже стоит до показа — кнопка сразу нажата", () => {
    document.documentElement.dataset.theme = "dark";
    render(<ThemeToggle />);
    expect(toggle()).toHaveAttribute("aria-pressed", "true");
  });

  it("хранилище не пишется — тема всё равно меняется, ошибки нет", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    render(<ThemeToggle />);
    fireEvent.click(toggle());
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(toggle()).toHaveAttribute("aria-pressed", "true");
  });
});
