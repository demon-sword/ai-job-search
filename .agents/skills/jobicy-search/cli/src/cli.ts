#!/usr/bin/env bun
// Self-contained CLI for searching Jobicy's public remote-jobs API.
// No external CLI framework and zero runtime dependencies, so it runs anywhere
// `bun` is available with nothing installed beyond the repo clone.
//
// Server-side filters (--tag, --geo, --industry) narrow the 7-day feed
// window; --query refinement and --jobage are applied client-side.

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

const HELP = `jobicy-cli — search Jobicy's public remote-jobs API (worldwide, remote-first)

Usage:
  jobicy-cli search [--tag <tag>] [--geo <code>] [--industry <slug>]
                    [-q <keywords>] [--jobage <days>]
                    [--page <n>] [--limit <n>] [--format json|table|plain]
                    [--description-format markdown|text] [--no-description]
  jobicy-cli detail <id|slug|url> [--format json|plain]

Search flags:
  --tag <tag>            server-side keyword filter on the job title
  --geo <code>           server-side country filter (e.g. usa, canada)
  --industry <slug>      server-side category filter (e.g. engineering)
  -q, --query <text>     keywords refined client-side (AND) across title,
                         company, excerpt, and description
  --jobage <days>        only jobs posted within N days (feed spans 7 days)
  --page <n>             1-indexed page, walked via cursor pagination (default 1)
  -n, --limit <n>        results per page, max 100 (default 25)
  --format <fmt>         json (default), table, or plain
  --description-format <fmt>  markdown (default) or text; json output only
  --no-description       drop bodies for a cheap discovery pass

Detail:
  <id|slug|url>          numeric job id (e.g. 161642), slug, or a full
                         https://jobicy.com/jobs/... posting URL
  --format <fmt>         json (default) or plain

Examples:
  jobicy-cli search --tag python --limit 10 --format table
  jobicy-cli search --geo usa --industry engineering
  jobicy-cli detail 161642 --format plain
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
    "query", "tag", "geo", "industry", "jobage", "page", "limit", "format",
    "description-format", "no-description", "help", "h",
  ]),
  detail: new Set(["format", "help", "h"]),
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

    const descFmt = stringFlag(flags["description-format"]) ?? "markdown";
    if (!["markdown", "text"].includes(descFmt)) {
      process.stderr.write(
        JSON.stringify({ error: `--description-format must be one of markdown|text, got "${descFmt}"`, code: "BAD_ARG" }) + "\n");
      return 1;
    }

    for (const name of ["jobage", "page", "limit"] as const) {
      if (flags[name] !== undefined) {
        const v = parseIntFlag(name, flags[name]!);
        if (v === null) return 1;
        flags[name] = String(v);
      }
    }
    if (flags.limit !== undefined && parseInt(flags.limit as string, 10) > 100) {
      process.stderr.write(JSON.stringify({ error: "--limit must be at most 100", code: "BAD_ARG" }) + "\n");
      return 1;
    }

    const opts: SearchOpts = {
      query: stringFlag(flags.query),
      tag: stringFlag(flags.tag),
      geo: stringFlag(flags.geo),
      industry: stringFlag(flags.industry),
      jobage: flags.jobage ? parseInt(flags.jobage as string, 10) : 9999,
      page: flags.page ? Math.max(1, parseInt(flags.page as string, 10)) : 1,
      limit: flags.limit ? Math.max(1, parseInt(flags.limit as string, 10)) : 25,
      format: fmt as SearchOpts["format"],
      descriptionFormat: descFmt as SearchOpts["descriptionFormat"],
      includeDescription: flags["no-description"] === undefined,
    };
    return runSearch(opts);
  }

  if (cmd === "detail") {
    const id = (flags._ as string[])[1];
    if (!id) {
      process.stderr.write(JSON.stringify({ error: "detail requires a <id|slug|url>", code: "NO_ID" }) + "\n");
      return 1;
    }
    const fmt = stringFlag(flags.format) ?? "json";
    const opts: DetailOpts = { id, format: fmt === "plain" ? "plain" : "json" };
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
