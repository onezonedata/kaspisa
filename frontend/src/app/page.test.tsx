import { render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { jsonResponse, stubFetch } from "@/test/http";
import Home from "./page";

const router = vi.hoisted(() => ({ replace: vi.fn(), push: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => router }));

let fetchMock: ReturnType<typeof stubFetch>;

beforeEach(() => {
  fetchMock = stubFetch();
  router.replace.mockReset();
});

afterEach(() => vi.unstubAllGlobals());

describe("главная страница", () => {
  it("вошедшего ведёт в /shops", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { id: "u1", email: "a@b.kz" }));
    render(<Home />);
    await vi.waitFor(() => expect(router.replace).toHaveBeenCalledWith("/shops"));
  });

  it("невошедшего ведёт на /login", async () => {
    fetchMock.mockResolvedValue(jsonResponse(401, { code: "unauthenticated" }));
    render(<Home />);
    await vi.waitFor(() => expect(router.replace).toHaveBeenCalledWith("/login"));
  });
});
