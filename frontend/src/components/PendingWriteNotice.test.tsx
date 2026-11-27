import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { t } from "@/i18n";
import { formatPrice } from "@/lib/format";
import { makePendingWrite } from "@/test/fixtures";
import { PendingWriteNotice } from "./PendingWriteNotice";

describe("PendingWriteNotice (Т37)", () => {
  it("null — ничего", () => {
    const { container } = render(<PendingWriteNotice pending={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("квитанция: цена, статус, hold_until, без предупреждения", () => {
    const { container } = render(<PendingWriteNotice pending={makePendingWrite()} />);
    const text = container.textContent ?? "";
    expect(text).toContain(formatPrice(1199));
    expect(text).toContain(t.pendingStatus.receipt);
    expect(text).toContain(t.ui.pendingHoldUntil);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("outside_bounds — предупреждение role=alert «вне новых границ»", () => {
    render(<PendingWriteNotice pending={makePendingWrite({ outside_bounds: true })} />);
    expect(screen.getByRole("alert")).toHaveTextContent("вне новых границ");
    expect(screen.getByRole("alert")).toHaveTextContent(t.ui.pendingOutsideBounds);
  });

  it("разные цены по городам — показаны обе", () => {
    const { container } = render(
      <PendingWriteNotice pending={makePendingWrite({ prices: { a: 1199, b: 1250 } })} />,
    );
    const text = container.textContent ?? "";
    expect(text).toContain(formatPrice(1199));
    expect(text).toContain(formatPrice(1250));
    expect(text).toContain("a");
    expect(text).toContain("b");
  });
});
