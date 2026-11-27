import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { t } from "@/i18n";
import { SOURCE_HREF } from "@/lib/source";
import NotFound from "./not-found";

describe("страница 404 (AGPL §13)", () => {
  it("есть ссылка на исходный код и ссылка в кабинет", () => {
    render(<NotFound />);
    expect(screen.getByRole("link", { name: t.ui.sourceCode })).toHaveAttribute("href", SOURCE_HREF);
    expect(screen.getByRole("heading", { name: t.ui.notFoundTitle })).toBeInTheDocument();
    expect(screen.getAllByRole("contentinfo")).toHaveLength(1);
  });
});
