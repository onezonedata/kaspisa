"use client";

import Link from "next/link";
import { useCallback, useLayoutEffect, useState, type FormEvent } from "react";

import { listProducts, updateProduct, type ApiError } from "@/api/client";
import { fill, refusalText, t } from "@/i18n";
import { boundPrice, checkNotice, checkTone, compactCheckNotice, countText, lastCheckText, priceText, trackedPosition } from "@/lib/product-view";
import { useLoad } from "@/lib/use-load";
import { BulkBar } from "./BulkBar";
import { ErrorMessage } from "./ErrorMessage";
import { PriceRead } from "./PriceRead";
import { RefreshButton } from "./RefreshButton";

const LIMIT = 50;

type Tri = "" | "true" | "false";
interface Filters {
  q: string;
  tracked: Tri;
  hasBounds: Tri;
  hasRefusal: Tri;
}
const NO_FILTERS: Filters = { q: "", tracked: "", hasBounds: "", hasRefusal: "" };

const toBool = (value: Tri): boolean | undefined => (value === "" ? undefined : value === "true");

function TriSelect({
  label,
  value,
  onChange,
}: {
  label: string;
  value: Tri;
  onChange: (value: Tri) => void;
}) {
  return (
    <label>
      {label}
      <select value={value} onChange={(e) => onChange(e.target.value as Tri)}>
        <option value="">{t.ui.filterAll}</option>
        <option value="true">{t.ui.filterYes}</option>
        <option value="false">{t.ui.filterNo}</option>
      </select>
    </label>
  );
}

export function ProductsTable({
  shopId,
  initialTracked = "",
  reloadKey = 0,
}: {
  shopId: string;
  initialTracked?: Tri;
  /** Новый номер перечитывает список с текущим фильтром и страницей (после загрузки каталога). */
  reloadKey?: number;
}) {
  const [filters, setFilters] = useState<Filters>({ ...NO_FILTERS, tracked: initialTracked });
  const [orderHint, setOrderHint] = useState(false);
  const [qInput, setQInput] = useState("");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [rowBusy, setRowBusy] = useState<Set<number>>(new Set());
  const [actionError, setActionError] = useState<ApiError | null>(null);
  const [bulkMessage, setBulkMessage] = useState<string | null>(null);

  const load = useCallback(
    () =>
      listProducts(shopId, {
        q: filters.q,
        tracked: toBool(filters.tracked),
        hasBounds: toBool(filters.hasBounds),
        hasRefusal: toBool(filters.hasRefusal),
        limit: LIMIT,
        offset,
      }),
    // reloadKey не читается в теле: новый номер даёт новый load и перечитывает список.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [shopId, filters, offset, reloadKey],
  );
  const { data, error, loading, reload, setData } = useLoad(load);

  useLayoutEffect(
    () => () => {
      setActionError(null);
      setBulkMessage(null);
    },
    [],
  );

  // Выбор живёт в пределах страницы: смена поиска, фильтра и страницы его снимает.
  function resetView(next: Filters) {
    setFilters(next);
    setOffset(0);
    setSelected(new Set());
    setBulkMessage(null);
    setOrderHint(false);
  }
  function goTo(next: number) {
    setOrderHint(false);
    setOffset(next);
    setSelected(new Set());
    setBulkMessage(null);
  }
  function onSearch(event: FormEvent) {
    event.preventDefault();
    resetView({ ...filters, q: qInput.trim() });
  }

  async function toggleTracked(id: number, tracked: boolean) {
    setRowBusy((prev) => new Set(prev).add(id));
    setActionError(null);
    try {
      const updated = await updateProduct(shopId, id, { tracked: !tracked });
      // Функциональная форма: ответы приходят в любом порядке, страница могла смениться.
      setData(
        (prev) => prev && { ...prev, items: prev.items.map((p) => (p.id === id ? updated : p)) },
      );
      setOrderHint(true);
    } catch (e) {
      setActionError(e as ApiError);
    } finally {
      setRowBusy((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    }
  }

  const items = data?.items ?? [];
  const total = data?.total ?? 0;
  const filtered =
    filters.q !== "" || filters.tracked !== "" || filters.hasBounds !== "" || filters.hasRefusal !== "";
  const allSelected = items.length > 0 && items.every((p) => selected.has(p.id));

  const toggleRow = (id: number, on: boolean) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });

  return (
    <section>
      <div className="toolbar">
      <form className="toolbar-search" onSubmit={onSearch}>
        <label>
          <span className="sr-only">{t.ui.searchLabel}</span>
          <input
            type="search"
            placeholder={t.ui.searchLabel}
            maxLength={100}
            value={qInput}
            onChange={(e) => setQInput(e.target.value)}
          />
        </label>
        <button type="submit">{t.ui.searchAction}</button>
      </form>
      <div className="toolbar-filters">
        <TriSelect
          label={t.ui.filterTracked}
          value={filters.tracked}
          onChange={(v) => resetView({ ...filters, tracked: v })}
        />
        <TriSelect
          label={t.ui.filterHasBounds}
          value={filters.hasBounds}
          onChange={(v) => resetView({ ...filters, hasBounds: v })}
        />
        <TriSelect
          label={t.ui.filterHasRefusal}
          value={filters.hasRefusal}
          onChange={(v) => resetView({ ...filters, hasRefusal: v })}
        />
        <RefreshButton
          onRefresh={() => {
            setOrderHint(false);
            reload();
          }}
          busy={loading}
        />
      </div>
      </div>

      <ErrorMessage error={error} />
      <ErrorMessage error={actionError} />
      {orderHint ? <p role="status">{t.ui.orderHint}</p> : null}
      {bulkMessage !== null ? <p role="status">{bulkMessage}</p> : null}
      {data === null && loading ? <p>{t.ui.loading}</p> : null}

      {selected.size > 0 ? (
        <BulkBar
          shopId={shopId}
          selectedIds={[...selected]}
          onDone={(updated) => {
            setSelected(new Set());
            setBulkMessage(fill(t.ui.bulkDone, { n: updated }));
            setOrderHint(false);
            reload();
          }}
        />
      ) : null}

      {data !== null && total === 0 && !filtered ? (
        <p>
          <span>{t.ui.catalogEmpty}</span> <Link href={`/shops/${shopId}`}>{t.ui.toShop}</Link>
        </p>
      ) : null}

      {data !== null && !(total === 0 && !filtered) ? (
        <>
          <div className="table-scroll">
<table className="data-table">
            <thead>
              <tr>
                <th>
                  {t.ui.colSelect}
                  <label className="hit">
                    <input
                      type="checkbox"
                      aria-label={t.ui.selectAll}
                      checked={allSelected}
                      onChange={(e) =>
                        setSelected(e.target.checked ? new Set(items.map((p) => p.id)) : new Set())
                      }
                    />
                  </label>
                </th>
                <th>{t.ui.colTrack}</th>
                <th>{t.ui.colSku}</th>
                <th>{t.ui.colTitle}</th>
                <th className="num">{t.ui.colMin}</th>
                <th className="num">{t.ui.colMax}</th>
                <th className="num">{t.ui.colStep}</th>
                <th>{t.ui.colDecrease}</th>
                <th>{t.ui.colRaise}</th>
                <th className="num">{t.ui.colPrice}</th>
                <th className="num">{t.ui.colPosition}</th>
                <th className="num">{t.ui.colCompetitors}</th>
                <th className="num">{t.ui.colFirstPrice}</th>
                <th>{t.ui.colCheck}</th>
                <th>{t.ui.colPriceRead}</th>
                <th>{t.ui.colRefusal}</th>
              </tr>
            </thead>
            <tbody>
              {items.map((p) => {
                const tone = checkTone(p);
                const notice = checkNotice(p);
                const shortNotice = compactCheckNotice(p);
                return (
                <tr key={p.id} data-selected={selected.has(p.id) ? "true" : undefined}>
                  <td>
                    <label className="hit">
                      <input
                        type="checkbox"
                        aria-label={fill(t.ui.selectRow, { sku: p.sku })}
                        checked={selected.has(p.id)}
                        onChange={(e) => toggleRow(p.id, e.target.checked)}
                      />
                    </label>
                  </td>
                  <td>
                    <label className="check" data-tone={p.tracked ? undefined : "untracked"}>
                      <input
                        type="checkbox"
                        role="switch"
                        aria-label={fill(t.ui.trackToggle, { sku: p.sku })}
                        checked={p.tracked}
                        disabled={rowBusy.has(p.id)}
                        onChange={() => void toggleTracked(p.id, p.tracked)}
                      />{" "}
                      {p.tracked ? t.ui.trackedOn : t.ui.trackedOff}
                    </label>
                  </td>
                  <td className="sku">{p.sku}</td>
                  <td className="title-cell">
                    <Link href={`/shops/${shopId}/products/${p.id}`}>{p.title}</Link>
                  </td>
                  <td className="num">{boundPrice(p.min_price)}</td>
                  <td className="num">{boundPrice(p.max_price)}</td>
                  <td className="num">{p.step}</td>
                  <td>{p.auto_decrease ? t.ui.yes : t.ui.no}</td>
                  <td>{p.auto_raise ? t.ui.yes : t.ui.no}</td>
                  <td className="num price">{priceText(p.current_price)}</td>
                  <td className="num">{trackedPosition(p)}</td>
                  <td className="num">{countText(p.competitors_count)}</td>
                  <td className="num">{priceText(p.first_place_price)}</td>
                  <td className="text-cell">
                    {tone === null ? null : (
                      <span
                        data-tone={tone}
                        title={shortNotice === notice ? undefined : (notice ?? undefined)}
                      >
                        {shortNotice}
                      </span>
                    )}
                    {lastCheckText(p) === null ? null : <small> ({lastCheckText(p)})</small>}
                  </td>
                  <td>
                    <PriceRead
                      shopId={shopId}
                      product={p}
                      onRead={(result) =>
                        setData(
                          (prev) =>
                            prev && {
                              ...prev,
                              items: prev.items.map((x) =>
                                x.id === p.id ? { ...result, pending_write: null } : x,
                              ),
                            },
                        )
                      }
                    />
                  </td>
                  <td className="text-cell">
                    {p.refusal_reason === null ? null : (
                      <span data-tone="problem">{refusalText(p.refusal_reason)}</span>
                    )}
                  </td>
                </tr>
                );
              })}
            </tbody>
          </table>
          </div>
          <div className="pager">
            <p>
              {total === 0 || items.length === 0
                ? t.ui.noProducts
                : fill(t.ui.pageRange, { a: offset + 1, b: offset + items.length, total })}
            </p>
            <button type="button" disabled={offset === 0} onClick={() => goTo(Math.max(0, offset - LIMIT))}>
              {t.ui.pagePrev}
            </button>
            <button
              type="button"
              disabled={offset + LIMIT >= total}
              onClick={() => goTo(offset + LIMIT)}
            >
              {t.ui.pageNext}
            </button>
          </div>
        </>
      ) : null}
    </section>
  );
}
