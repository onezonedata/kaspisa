import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { jsonResponse, stubFetch } from "@/test/http";
import { t } from "@/i18n";
import { UpdateBanner } from "./UpdateBanner";

let fetchMock: ReturnType<typeof stubFetch>;

beforeEach(() => {
  fetchMock = stubFetch();
});

const available = {
  update_available: true,
  latest: "0.2.0",
  current: "0.1.0",
  release_url: "https://github.com/o/r/releases/tag/v0.2.0",
};

async function requested() {
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/version", expect.anything()));
}

describe("UpdateBanner (Т11–Т14)", () => {
  it("Т11: показывает версии и безопасную ссылку на релиз", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, available));
    render(<UpdateBanner />);
    const link = await screen.findByRole("link", { name: t.ui.updateHow });
    expect(link).toHaveAttribute("href", available.release_url);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
    expect(link.getAttribute("rel")).toContain("noreferrer");
    expect(screen.getByRole("status")).toHaveTextContent("0.2.0");
    expect(screen.getByRole("status")).toHaveTextContent("0.1.0");
  });

  it.each([
    ["обновления нет", { ...available, update_available: false }],
    ["latest пуст", { ...available, latest: null }],
  ])("Т12: %s — ничего не рисует", async (_name, body) => {
    fetchMock.mockResolvedValue(jsonResponse(200, body));
    const { container } = render(<UpdateBanner />);
    await requested();
    await new Promise((r) => setTimeout(r, 0));
    expect(container).toBeEmptyDOMElement();
  });

  it.each([
    ["401", () => Promise.resolve(jsonResponse(401, { code: "unauthenticated" }))],
    ["500", () => Promise.resolve(jsonResponse(500, { code: "internal_error" }))],
    ["сеть", () => Promise.reject(new TypeError("fail"))],
  ])("Т13: %s — ничего не рисует, ошибки нет", async (_name, answer) => {
    fetchMock.mockImplementation(answer);
    const { container } = render(<UpdateBanner />);
    await requested();
    await new Promise((r) => setTimeout(r, 0));
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("Т14: чужой адрес не становится ссылкой, текст остаётся", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, { ...available, release_url: "javascript:alert(1)" }),
    );
    render(<UpdateBanner />);
    expect(await screen.findByRole("status")).toHaveTextContent("0.2.0");
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});
