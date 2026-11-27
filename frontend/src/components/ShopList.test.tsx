import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { t } from "@/i18n";
import { makeShop } from "@/test/fixtures";
import { ShopList } from "./ShopList";

describe("ShopList (Т17)", () => {
  it("показывает название, состояние подключения и пометку остановки", () => {
    render(
      <ShopList
        shops={[
          makeShop({ id: "a", merchant_uid: "30000001", connection_state: "connected", halt_reason: "write_errors" }),
          makeShop({ id: "b", connection_state: "not_connected" }),
        ]}
      />,
    );
    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(within(items[0]).getByRole("link", { name: "30000001" })).toHaveAttribute("href", "/shops/a");
    expect(within(items[0]).getByText(t.connectionState.connected)).toBeInTheDocument();
    expect(within(items[0]).getByText(t.ui.botHalted)).toBeInTheDocument();
    expect(within(items[1]).getByText(new RegExp(t.ui.shopNoConnection))).toBeInTheDocument();
    expect(within(items[1]).getByText(t.connectionState.not_connected)).toBeInTheDocument();
    expect(within(items[1]).queryByText(t.ui.botHalted)).toBeNull();
  });

  it("пустой список — пояснение", () => {
    render(<ShopList shops={[]} />);
    expect(screen.getByText(t.ui.shopsEmpty)).toBeInTheDocument();
  });
});
