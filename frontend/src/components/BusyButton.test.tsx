import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { BusyButton } from "./BusyButton";

describe("BusyButton", () => {
  it("в покое показывает содержимое и реагирует на нажатие", () => {
    const onClick = vi.fn();
    render(
      <BusyButton busy={false} busyText="Ждём…" onClick={onClick}>
        Подключить
      </BusyButton>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Подключить" }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("при busy заблокирована и показывает текст ожидания", () => {
    const onClick = vi.fn();
    render(
      <BusyButton busy busyText="Ждём…" onClick={onClick}>
        Подключить
      </BusyButton>,
    );
    const button = screen.getByRole("button", { name: "Ждём…" });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(onClick).not.toHaveBeenCalled();
  });
});
