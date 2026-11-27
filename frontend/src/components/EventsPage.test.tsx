import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { OwnerEvent } from "@/api/types";
import { fill, t } from "@/i18n";
import { formatDateTime } from "@/lib/format";
import { jsonResponse, stubFetch } from "@/test/http";
import { EventsPage } from "./EventsPage";

const SHOP = "11111111-1111-4111-8111-111111111111";
let fetchMock: ReturnType<typeof stubFetch>;
beforeEach(() => {
  fetchMock = stubFetch();
});
afterEach(() => vi.unstubAllGlobals());

const event = (o: Partial<OwnerEvent>): OwnerEvent => ({
  id: 1,
  kind: "login",
  ip: "203.0.113.7",
  shop_id: null,
  sku: null,
  old_value: null,
  new_value: null,
  user_email: "boss@example.kz",
  created_at: "2026-11-19T10:00:00Z",
  ...o,
});

describe("EventsPage: пользователи и «Кто» (Т36)", () => {
  it("user_added — адрес из new_value, user_removed — из old_value; колонка «Кто»", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, [
        event({ id: 2, kind: "user_removed", old_value: "gone@example.kz", user_email: "other@example.kz" }),
        event({ id: 1, kind: "user_added", new_value: "added@example.kz" }),
      ]),
    );
    render(<EventsPage />);
    expect(await screen.findByText(t.ownerEvent.user_added)).toBeInTheDocument();
    expect(screen.getByText(t.ownerEvent.user_removed)).toBeInTheDocument();
    expect(screen.getByText("added@example.kz")).toBeInTheDocument();
    expect(screen.getByText("gone@example.kz")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: t.ui.colEventUser })).toBeInTheDocument();
    expect(screen.getByText("boss@example.kz")).toBeInTheDocument();
    expect(screen.getByText("other@example.kz")).toBeInTheDocument();
  });
});

describe("EventsPage (Т32)", () => {
  it("строки четырёх видов: вид, адрес, время и значения", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, [
        event({ id: 4, kind: "bounds_changed", shop_id: SHOP, sku: "SKU-9", old_value: "1000–5000", new_value: "800–5000", ip: "198.51.100.4" }),
        event({ id: 3, kind: "bot_mode_changed", shop_id: SHOP, old_value: "off", new_value: "live" }),
        event({ id: 2, kind: "password_confirm_failed" }),
        event({ id: 1, kind: "login" }),
      ]),
    );
    render(<EventsPage />);
    expect(await screen.findByText(t.ownerEvent.login)).toBeInTheDocument();
    expect(screen.getByText(t.ownerEvent.password_confirm_failed)).toBeInTheDocument();
    expect(screen.getByText(t.ownerEvent.bot_mode_changed)).toBeInTheDocument();
    expect(screen.getByText(t.ownerEvent.bounds_changed)).toBeInTheDocument();
    expect(
      screen.getByText(fill(t.ui.eventModeChange, { old: t.botMode.off, new: t.botMode.live })),
    ).toBeInTheDocument();
    expect(
      screen.getByText(fill(t.ui.eventBoundsChange, { sku: "SKU-9", old: "1000–5000", new: "800–5000" })),
    ).toBeInTheDocument();
    expect(screen.getByText("198.51.100.4")).toBeInTheDocument();
    expect(screen.getAllByText("203.0.113.7")).toHaveLength(3);
    expect(screen.getAllByText(formatDateTime("2026-11-19T10:00:00Z"))).toHaveLength(4);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/events");
  });

  it("пустой журнал — «Событий нет»", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, []));
    render(<EventsPage />);
    expect(await screen.findByText(t.ui.eventsEmpty)).toBeInTheDocument();
    expect(t.ui.eventsEmpty).toBe("Событий нет");
  });

  it("ошибка загрузки — текст ошибки", async () => {
    fetchMock.mockResolvedValue(jsonResponse(500, { code: "internal_error" }));
    render(<EventsPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent(t.errors.internal_error);
  });
});
