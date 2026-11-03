#!/usr/bin/env bun
// job-check CLI: one command that runs discovery + dedupe + full-text triage
// and emits the ranked shortlist with pay-vs-bar and India-eligibility
// verdicts. Zero runtime dependencies — plain `bun` + `fetch`.

import { runCheck, type CheckOpts } from "./commands/check.js";
import {
  DEFAULT_MIN_SCORE,
  DEFAULT_PAY_BAR_LPA,
  formatPlain,
  formatTable,
} from "./helpers.js";

interface Flags {
  _: string[];
  [k: string]: string | boolean | string[];
}

function parseFlags(argv: string[]): Flags {
  const flags: Flags = { _: [] };
  const alias: Record<string, string> = { q: "query", l: "location", n: "limit" };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith("--") || a.startsWith("-")) {
      const key = alias[a.replace(/^-+/, "")] ?? a.replace(/^-+/, "");
      const next = argv[i + 1];
      if (next === undefined || next.startsWith("-")) {
        flags[key] = true;
      } else {
        flags[key] = next;
        i++;
      }
    } else {
      (flags._ as string[]).push(a);
    }
  }
  return flags;
}

function fail(message: string): never {
  console.error(JSON.stringify({ error: message, code: "BAD_ARG" }));
  process.exit(1);
}

function strFlag(flags: Flags, name: string): string | undefined {
  const v = flags[name];
  if (v === undefined || v === true) return undefined;
  return v as string;
}

function intFlag(flags: Flags, name: string): number | undefined {
  const raw = strFlag(flags, name);
  if (raw === undefined) return undefined;
  if (!/^\d+$/.test(raw)) fail(`--${name} must be a non-negative integer (got ${JSON.stringify(raw)})`);
  return Number(raw);
}

const HELP = `job-check — check live jobs and emit a ranked shortlist

USAGE
  bun run src/cli.ts check [flags]
  bun run src/cli.ts check --input <jobs.json> [flags]

CHECK FLAGS
  --query, -q <text>      Keywords (role/skill). Default "software engineer".
  --location, -l <text>   Place scope for portal searches. Default "India".
  --jobage <days>         Posted within N days (portals that support it).
  --limit, -n <n>         Cap candidates triaged per run. Default 20.
  --portal <a,b>          Restrict discovery to named portal skill(s).
  --input <path>          Offline mode: triage candidates from a JSON file
                          [{title, company, location, url, text?, snippet?}]
                          instead of live discovery. No network needed.
  --cache-dir <path>      Detail-response cache dir. Default
                          .agents/skills/job-check/.cache
  --no-cache              Bypass the detail cache.
  --pay-bar-lpa <n>       Pay bar in LPA. Default 55.
  --min-score <n>         Shortlist floor 0-100. Default 45 (Moderate Fit).
  --format <fmt>          json (default) | table | plain.

EXAMPLES
  bun run src/cli.ts check -q "data engineer" -l "India" --format table
  bun run src/cli.ts check --input /tmp/jobs.json --pay-bar-lpa 55 --format table
`;

async function main(): Promise<void> {
  const flags = parseFlags(process.argv.slice(2));
  const [command] = flags._ as string[];

  if (flags["help"] === true || flags["h"] === true || command === "help") {
    console.log(HELP);
    return;
  }

  if (command !== "check") {
    fail(command ? `unknown command ${JSON.stringify(command)} (expected "check")` : "no command given (expected \"check\")");
  }

  for (const key of Object.keys(flags)) {
    if (
      key !== "_" &&
      ![
        "query", "location", "jobage", "limit", "portal", "input",
        "cache-dir", "no-cache", "pay-bar-lpa", "min-score", "format",
      ].includes(key)
    ) {
      fail(`unknown flag --${key}`);
    }
  }

  const format = strFlag(flags, "format") ?? "json";
  if (!["json", "table", "plain"].includes(format)) {
    fail(`--format must be json|table|plain (got ${JSON.stringify(format)})`);
  }

  const opts: CheckOpts = {
    query: strFlag(flags, "query") ?? "software engineer",
    location: strFlag(flags, "location") ?? "India",
    jobage: intFlag(flags, "jobage"),
    limit: intFlag(flags, "limit") ?? 20,
    portals: strFlag(flags, "portal")?.split(",").map((s) => s.trim()).filter(Boolean),
    input: strFlag(flags, "input"),
    cacheDir:
      strFlag(flags, "cache-dir") ??
      ".agents/skills/job-check/.cache",
    noCache: flags["no-cache"] === true,
    payBarLpa: intFlag(flags, "pay-bar-lpa") ?? DEFAULT_PAY_BAR_LPA,
    minScore: intFlag(flags, "min-score") ?? DEFAULT_MIN_SCORE,
  };

  try {
    const report = await runCheck(opts);
    if (format === "json") console.log(JSON.stringify(report, null, 2));
    else if (format === "table") console.log(formatTable(report));
    else console.log(formatPlain(report));
  } catch (err) {
    console.error(
      JSON.stringify({ error: err instanceof Error ? err.message : String(err), code: "RUN_FAILED" })
    );
    process.exit(1);
  }
}

await main();
