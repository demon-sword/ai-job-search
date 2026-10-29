import { readFileSync } from "fs";

export function writeError(error: string, code: string): void {
  process.stderr.write(JSON.stringify({ error, code }) + "\n");
}

export function fail(error: string, code: string): never {
  writeError(error, code);
  process.exit(1);
}

export function readJsonFile<T>(path: string, what: string): T {
  try {
    return JSON.parse(readFileSync(path, "utf-8")) as T;
  } catch {
    fail(`cannot read ${what} at ${path}`, "BAD_ARGS");
  }
}

export function localDay(ts: string): string {
  // Calendar day in the runner's local timezone: receipts cap per local day.
  const d = new Date(ts);
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

export function stampForFilename(date = new Date()): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return (
    `${date.getFullYear()}${p(date.getMonth() + 1)}${p(date.getDate())}` +
    `T${p(date.getHours())}${p(date.getMinutes())}${p(date.getSeconds())}`
  );
}
