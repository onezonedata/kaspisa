"use client";

import Link from "next/link";

import { listEvents } from "@/api/client";
import type { OwnerEvent } from "@/api/types";
import { codeText, fill, t } from "@/i18n";
import { formatDateTime } from "@/lib/format";
import { useLoad } from "@/lib/use-load";
import { ErrorMessage } from "./ErrorMessage";
import { RefreshButton } from "./RefreshButton";

const DASH = "—";

/** Подробности события: режимы названы по словарю, границы — как прислал сервер. */
function detail(event: OwnerEvent): string {
  if (event.kind === "bot_mode_changed") {
    return fill(t.ui.eventModeChange, {
      old: event.old_value === null ? DASH : codeText(t.botMode, event.old_value),
      new: event.new_value === null ? DASH : codeText(t.botMode, event.new_value),
    });
  }
  if (event.kind === "bounds_changed") {
    return fill(t.ui.eventBoundsChange, {
      sku: event.sku ?? DASH,
      old: event.old_value ?? DASH,
      new: event.new_value ?? DASH,
    });
  }
  if (event.kind === "user_added") return event.new_value ?? DASH;
  if (event.kind === "user_removed") return event.old_value ?? DASH;
  return DASH;
}

/** Журнал действий владельца: последние события, новые первыми (ADR 0024). */
export function EventsPage() {
  const { data: events, error, loading, reload } = useLoad(listEvents);
  return (
    <main className="page">
      <h1>{t.ui.eventsTitle}</h1>
      <nav>
        <Link href="/shops">{t.ui.eventsToShops}</Link>
      </nav>
      <RefreshButton onRefresh={reload} busy={loading} />
      <ErrorMessage error={error} />
      {events === null && loading ? <p>{t.ui.loading}</p> : null}
      {events !== null && events.length === 0 ? <p>{t.ui.eventsEmpty}</p> : null}
      {events !== null && events.length > 0 ? (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>{t.ui.colEventTime}</th>
                <th>{t.ui.colEventKind}</th>
                <th>{t.ui.colEventDetail}</th>
                <th>{t.ui.colEventUser}</th>
                <th>{t.ui.colEventIp}</th>
              </tr>
            </thead>
            <tbody>
              {events.map((event) => (
                <tr key={event.id}>
                  <td>{formatDateTime(event.created_at)}</td>
                  <td>{codeText(t.ownerEvent, event.kind)}</td>
                  <td className="wrap">{detail(event)}</td>
                  <td className="wrap">{event.user_email}</td>
                  <td className="sku">{event.ip}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </main>
  );
}
