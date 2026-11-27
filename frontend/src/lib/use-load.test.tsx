import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ApiError } from "@/api/client";
import { useLoad } from "./use-load";

describe("useLoad", () => {
  it("reload: пока идёт повторная загрузка, loading=true; после — false", async () => {
    let finish: (v: string) => void = () => {};
    const calls: Array<Promise<string>> = [Promise.resolve("a")];
    const load = () => calls.shift() ?? new Promise<string>((r) => (finish = r));
    const { result } = renderHook(() => useLoad(load));
    await waitFor(() => expect(result.current.loading).toBe(false));
    act(() => result.current.reload());
    expect(result.current.loading).toBe(true);
    finish("b");
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toBe("b");
  });

  it("смена загрузчика: старая ошибка и данные не показываются, идёт loading", async () => {
    const failing = () => Promise.reject(new ApiError(503, "server_busy"));
    let finish: (v: string) => void = () => {};
    const pending = () => new Promise<string>((r) => (finish = r));
    const { result, rerender } = renderHook(({ load }) => useLoad<string>(load), {
      initialProps: { load: failing as () => Promise<string> },
    });
    await waitFor(() => expect(result.current.error).not.toBeNull());
    rerender({ load: pending });
    expect(result.current.error).toBeNull();
    expect(result.current.data).toBeNull();
    expect(result.current.loading).toBe(true);
    finish("новое");
    await waitFor(() => expect(result.current.data).toBe("новое"));
  });
});
