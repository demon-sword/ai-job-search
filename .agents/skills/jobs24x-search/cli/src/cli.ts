#!/usr/bin/env bun
// Self-contained CLI for reading jobs24x's job listing through the site's own
// encrypted edge functions. Zero runtime dependencies (WebCrypto for AES-GCM).
//
// Anonymous calls return LOCKED rows only (no company/URL); a user-supplied
// JOBS24X_API_TOKEN is passed through as the bearer header for paid sessions.
// No accounts are created and nothing is paid for by this skill.

import { runSearch, type SearchOpts } from "./commands/search.js";
import { runDetail, type DetailOpts } from "./commands/detail.js";

interface Flags {
  _: string[];
  [key: string]: string | boolean | string[] | undefined;
}

// Short-flag aliases.
const ALIAS: Record<string, string> = { q: "query", n: "limit" };

function parseFlags(argv: string[]): Flags {
  const flags: Flags = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (!a.startsWith("-")) {
      (flags._ as string[]).push(a);
      continue;
    }
    const name = a.replace(/^-+/, "");
    const key = ALIAS[name] ?? name;
    const next = argv[i + 1];
    // A flag with no following value (or another flag next) is a boolean.
    let value: string | boolean = true;
    if (next !== undefined && !next.startsWith("-")) {
      value = next;
      i++;
    }
    flags[key] = value;
  }
  return flags;
}

type FlagValue = string | boolean | string[] | undefined;

/** A flag's string value. A bare flag yields `whenBare`. */
function stringFlag(raw: FlagValue, whenBare?: string): string | undefined {
  if (typeof raw === "string") return raw;
  if (Array.isArray(raw)) return raw.length ? raw[raw.length - 1] : undefined;
  if (raw) return whenBare;
  return undefined;
}

const HELP = `jobs24x-cli — read jobs24x's job listing (free surface is paywalled: locked rows)

Usage:
  jobs24x-cli search [-q <keywords>] [--remote <mode>]
                     [--employment-type <type>] [--country <code>]
                     [--page <n>] [--limit <n>] [--format json|table|plain]
  jobs24x-cli detail <uuid> [--format json|plain] [--max-pages <n>]

Search flags:
  -q, --query <text>        keyword search
  --remote <mode>           e.g. Remote, On-site, Hybrid (site vocabulary)
  --employment-type <type>  e.g. Full-time, Contract (site vocabulary)
  --country <code>          country filter (site vocabulary)
  --jobage <days>           client-side recency filter over the returned page
  --page <n>                1-indexed page (default 1)
  -n, --limit <n>           results per page (default 12, the site page size)
  --format <fmt>            json (default), table, or plain

Detail:
  <uuid>                    posting UUID (the id from a search result)
  --query/--remote/--employment-type/--country
                            replay the listing query that surfaced the UUID
                            (list-jobs has no per-job lookup)
  --max-pages <n>           listing pages to walk looking for the UUID
                            (default 10)
  --format <fmt>            json (default) or plain

Paid sessions:
  JOBS24X_API_TOKEN         your own paid-session Supabase access token,
                            sent as the bearer header (never a flag)

Examples:
  jobs24x-cli search -q "python" --limit 5 --format table
  jobs24x-cli detail eb7ed263-0eb0-4c0f-a602-be06f92be --format plain
`;

function parseIntFlag(name: string, raw: string | boolean | string[]): number | null {
  const s = Array.isArray(raw) ? raw[raw.length - 1] : raw;
  if (typeof s !== "string" || !/^\d+$/.test(s)) {
    process.stderr.write(JSON.stringify({ error: `--${name} must be a positive integer, got "${s}"`, code: "BAD_ARG" }) + "\n");
    return null;
  }
  const v = parseInt(s, 10);
  if (v <= 0) {
    process.stderr.write(JSON.stringify({ error: `--${name} must be a positive integer, got "${s}"`, code: "BAD_ARG" }) + "\n");
    return null;
  }
  return v;
}

// Long-form flag names each command accepts (parseFlags resolves the short
// aliases q/n to these before validation). "help"/"h" pass so `search --help`
// still prints usage.
const KNOWN_FLAGS: Record<string, Set<string>> = {
  search: new Set([
    "query", "remote", "employment-type", "country", "jobage", "page", "limit", "format", "help", "h",
  ]),
  detail: new Set(["format", "max-pages", "query", "remote", "employment-type", "country", "help", "h"]),
};

async function main(): Promise<number> {
  const argv = process.argv.slice(2);
  const flags = parseFlags(argv);
  const cmd = (flags._ as string[])[0];

  if (!cmd || flags.help || flags.h) {
    process.stdout.write(HELP);
    return cmd ? 0 : 1;
  }

  // Reject unknown flags instead of silently discarding them: a discarded
  // filter changes what the search returns with no error (a wrong flag name
  // once returned an entire portal's database as if it matched the query).
  // add-portal.md's contract requires a bogus flag to exit 1 with a JSON
  // error on stderr.
  const knownFlags = KNOWN_FLAGS[cmd];
  if (knownFlags) {
    for (const key of Object.keys(flags)) {
      if (key === "_" || knownFlags.has(key)) continue;
      process.stderr.write(
        JSON.stringify({
          error: `unknown flag --${key} for '${cmd}' - flags are never silently ignored, because a discarded filter changes what the search returns; see --help for the supported flags`,
          code: "UNKNOWN_FLAG",
        }) + "\n",
      );
      return 1;
    }
  }

  if (cmd === "search") {
    const fmt = stringFlag(flags.format) ?? "json";
    if (!["json", "table", "plain"].includes(fmt)) {
      process.stderr.write(JSON.stringify({ error: `--format must be one of json|table|plain, got "${fmt}"`, code: "BAD_ARG" }) + "\n");
      return 1;
    }

    for (const name of ["jobage", "page", "limit"] as const) {
      if (flags[name] !== undefined) {
        const v = parseIntFlag(name, flags[name]!);
        if (v === null) return 1;
        flags[name] = String(v);
      }
    }

    const opts: SearchOpts = {
      query: stringFlag(flags.query),
      remote: stringFlag(flags.remote),
      employmentType: stringFlag(flags["employment-type"]),
      country: stringFlag(flags.country),
      jobage: flags.jobage ? parseInt(flags.jobage as string, 10) : 9999,
      page: flags.page ? Math.max(1, parseInt(flags.page as string, 10)) : 1,
      limit: flags.limit ? Math.max(1, parseInt(flags.limit as string, 10)) : 12,
      format: fmt as SearchOpts["format"],
    };
    return runSearch(opts);
  }

  if (cmd === "detail") {
    const id = (flags._ as string[])[1];
    if (!id) {
      process.stderr.write(JSON.stringify({ error: "detail requires a <uuid>", code: "NO_ID" }) + "\n");
      return 1;
    }
    let maxPages = 10;
    if (flags["max-pages"] !== undefined) {
      const v = parseIntFlag("max-pages", flags["max-pages"]!);
      if (v === null) return 1;
      maxPages = v;
    }
    const fmt = stringFlag(flags.format) ?? "json";
    const opts: DetailOpts = { id, format: fmt === "plain" ? "plain" : "json", maxPages, query: stringFlag(flags.query), remote: stringFlag(flags.remote), employmentType: stringFlag(flags["employment-type"]), country: stringFlag(flags.country) };
    return runDetail(opts);
  }

  process.stderr.write(JSON.stringify({ error: `Unknown command "${cmd}"`, code: "BAD_CMD" }) + "\n");
  return 1;
}

main()
  .then((code) => process.exit(code))
  .catch((e) => {
    process.stderr.write(
      JSON.stringify({
        error: e instanceof Error ? e.message : String(e),
        code: "INTERNAL_ERROR",
      }) + "\n",
    );
    process.exit(1);
  });

