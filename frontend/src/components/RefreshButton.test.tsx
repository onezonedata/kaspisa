import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { t } from "@/i18n";
import { RefreshButton } from "./RefreshButton";

describe("RefreshButton", () => {
  it("вызывает onRefresh", () => {
    const onRefresh = vi.fn();
    render(<RefreshButton onRefresh={onRefresh} busy={false} />);
    fireEvent.click(screen.getByRole("button", { name: t.ui.refresh }));
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it("во время обновления заблокирована", () => {
    render(<RefreshButton onRefresh={vi.fn()} busy />);
    expect(screen.getByRole("button")).toBeDisabled();
  });
});
