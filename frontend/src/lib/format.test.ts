import { describe, expect, it } from "vitest";

import { formatPrice } from "./format";

const plain = (s: string) => s.replace(/\s/g, " ");

describe("formatPrice", () => {
  it("целые тенге с разделителем разрядов и знаком", () => {
    expect(plain(formatPrice(12990))).toBe("12 990 ₸");
    expect(plain(formatPrice(5))).toBe("5 ₸");
  });

  it("не целое число не показывается как цена", () => {
    expect(formatPrice(1.5)).toBe("—");
  });
});
