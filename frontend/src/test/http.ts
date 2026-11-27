// Помощники тестов: ответы fetch и мок next/navigation.
import { vi } from "vitest";

export function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export function emptyResponse(status: number): Response {
  return new Response(null, { status });
}

export function htmlResponse(status: number): Response {
  return new Response("<html>Bad gateway</html>", {
    status,
    headers: { "Content-Type": "text/html" },
  });
}

/** Ставит глобальный fetch-мок и возвращает его. */
export function stubFetch(): ReturnType<typeof vi.fn> {
  const mock = vi.fn();
  vi.stubGlobal("fetch", mock);
  return mock;
}

/** Тело запроса, переданное в n-й вызов fetch, как объект. */
export function sentBody(mock: ReturnType<typeof vi.fn>, call = 0): unknown {
  const init = mock.mock.calls[call][1] as RequestInit;
  return JSON.parse(init.body as string);
}
