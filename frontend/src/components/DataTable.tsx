import { useState, type ReactNode } from "react";
import {
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type ColumnDef,
  type SortingState,
} from "@tanstack/react-table";
import { Tooltip } from "@mantine/core";

export type Col<T> = {
  id: string;
  header: string;
  /** Value used for sorting (and default rendering). */
  value?: (row: T) => number | string | null | undefined;
  cell?: (row: T) => ReactNode;
  align?: "left" | "right" | "center";
  tip?: string;
  width?: number;
  sortable?: boolean;
};

export function DataTable<T>({
  data, cols, onRowClick, initialSort, maxHeight = 520, rowKey, rowClass, empty = "No rows", dense = true,
}: {
  data: T[];
  cols: Col<T>[];
  onRowClick?: (row: T) => void;
  initialSort?: SortingState;
  maxHeight?: number | string;
  rowKey?: (row: T, i: number) => string;
  rowClass?: (row: T) => string | undefined;
  empty?: string;
  dense?: boolean;
}) {
  const [sorting, setSorting] = useState<SortingState>(initialSort ?? []);
  const columns: ColumnDef<T>[] = cols.map((c) => ({
    id: c.id,
    header: c.header,
    accessorFn: (r: T) => (c.value ? c.value(r) : null),
    cell: (info) => (c.cell ? c.cell(info.row.original) : (info.getValue() as ReactNode) ?? "—"),
    enableSorting: c.sortable !== false && !!c.value,
    sortUndefined: "last",
    sortingFn: (a, b, id) => {
      const x = a.getValue(id) as number | string | null;
      const y = b.getValue(id) as number | string | null;
      if (x == null && y == null) return 0;
      if (x == null) return 1;
      if (y == null) return -1;
      return x < y ? -1 : x > y ? 1 : 0;
    },
    meta: c,
  }));
  const table = useReactTable({
    data,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getRowId: rowKey ? (r, i) => rowKey(r, i) : undefined,
  });

  return (
    <div className="dt-wrap" style={{ maxHeight }}>
      <table className={`dt ${dense ? "dense" : ""}`}>
        <thead>
          {table.getHeaderGroups().map((hg) => (
            <tr key={hg.id}>
              {hg.headers.map((h) => {
                const meta = h.column.columnDef.meta as Col<T>;
                const sorted = h.column.getIsSorted();
                const label = (
                  <span>
                    {meta.header}
                    {sorted === "asc" ? " ▲" : sorted === "desc" ? " ▼" : ""}
                  </span>
                );
                return (
                  <th
                    key={h.id}
                    style={{ textAlign: meta.align ?? "left", width: meta.width }}
                    className={h.column.getCanSort() ? "sortable" : ""}
                    onClick={h.column.getToggleSortingHandler()}
                    aria-sort={sorted === "asc" ? "ascending" : sorted === "desc" ? "descending" : "none"}
                  >
                    {meta.tip ? (
                      <Tooltip label={meta.tip} multiline w={260} withArrow>
                        <span className="th-tip">{label}</span>
                      </Tooltip>
                    ) : (
                      label
                    )}
                  </th>
                );
              })}
            </tr>
          ))}
        </thead>
        <tbody>
          {table.getRowModel().rows.length === 0 && (
            <tr>
              <td colSpan={cols.length} className="dt-empty">{empty}</td>
            </tr>
          )}
          {table.getRowModel().rows.map((row) => (
            <tr
              key={row.id}
              onClick={onRowClick ? () => onRowClick(row.original) : undefined}
              className={`${onRowClick ? "clickable" : ""} ${rowClass?.(row.original) ?? ""}`}
            >
              {row.getVisibleCells().map((cell) => {
                const meta = cell.column.columnDef.meta as Col<T>;
                return (
                  <td key={cell.id} style={{ textAlign: meta.align ?? "left" }} className={meta.align === "right" ? "num" : ""}>
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
