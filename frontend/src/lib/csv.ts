// Client-side CSV export for the matched-jobs table. This is NOT a backend endpoint —
// the Streamlit dashboard's "Export matched jobs CSV" button also builds the CSV in the
// browser from the already-loaded rows.

export interface CsvColumn<T> {
  key: keyof T;
  label: string;
}

function cell(value: unknown): string {
  if (value === null || value === undefined) return "";
  const s = String(value);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

export function downloadCsv<T>(filename: string, rows: T[], columns: CsvColumn<T>[]): void {
  const header = columns.map((c) => c.label).join(",");
  const body = rows.map((row) => columns.map((c) => cell(row[c.key])).join(",")).join("\n");
  const csv = `${header}\n${body}`;
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}
