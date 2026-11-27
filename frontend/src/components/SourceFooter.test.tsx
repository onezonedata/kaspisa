import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { APP_VERSION, SOURCE_HREF, SOURCE_REPO_URL } from "@/lib/source";
import { SourceFooter } from "./SourceFooter";

describe("SourceFooter (Т12)", () => {
  it("ссылка на метку выпуска, открывается безопасно", () => {
    render(<SourceFooter />);
    const link = screen.getByRole("link", { name: "Исходный код" });
    expect(link).toHaveAttribute("href", `${SOURCE_REPO_URL}/tree/v${APP_VERSION}`);
    expect(link).toHaveAttribute("href", "https://github.com/onezonedata/kaspisa/tree/v0.2.0");
    expect(SOURCE_HREF).toBe(link.getAttribute("href"));
    expect(link).toHaveAttribute("target", "_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
    expect(link.getAttribute("rel")).toContain("noreferrer");
  });

  it("в подвале есть версия", () => {
    const { container } = render(<SourceFooter />);
    expect(container.querySelector("footer")?.textContent).toContain("0.2.0");
  });
});
