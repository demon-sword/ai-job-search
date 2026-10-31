#!/usr/bin/env python3
"""Scheduled-sweep runner: cadence gate, then ``tools/sweep_auto.py run``.

Thin entrypoint for cron or a scheduled workflow job. Scheduled invocations
inside the cadence window exit quietly (code 0, one line); when the sweep is
due - or ``--force`` is passed - the real work (discovery, content-hash
dedupe, triage, artifact + tracker + state writes) is delegated to
``tools/sweep_auto.py run``, and the artifact path is printed for the caller
to archive or upload.

Usage:
    python3 tools/sweep-runner.py            # run only when due
    python3 tools/sweep-runner.py --force    # run regardless
    python3 tools/sweep-runner.py --date 2026-10-31 --force

Cron example (weekly Monday 09:00; the cadence gate below decides):
    0 9 * * 1 cd /path/to/ai-job-search && python3 tools/sweep-runner.py

Note: discovery shells out to the portal CLIs, and the linkedin-search skill
is personal-use only per its own ToS warning - schedule this where the
operator's own session runs (local cron), not on shared CI runners.
"""

import argparse
import datetime as _dt
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import sweep_auto  # noqa: E402  (repo-local tool, resolved via ROOT above)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true",
                    help="run even when the cadence window has not elapsed")
    ap.add_argument("--date", default=None,
                    help="run date YYYY-MM-DD (default today)")
    ap.add_argument("--cadence-days", type=int, default=sweep_auto.CADENCE_DAYS)
    ap.add_argument("--seen", default=str(sweep_auto.SEEN))
    ap.add_argument("--tracker", default=str(sweep_auto.TRACKER))
    ap.add_argument("--state", default=str(sweep_auto.SCHEDULER_STATE))
    ap.add_argument("--artifacts", default=str(sweep_auto.ARTIFACT_DIR))
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    run_date = args.date or _dt.date.today().isoformat()
    if not args.force and not sweep_auto.is_due(
            args.cadence_days, today=run_date, state_path=args.state):
        print(f"sweep-auto: not due (last run {sweep_auto.last_run(args.state)}); "
              f"use --force to run anyway")
        return 0
    cmd = [sys.executable, str(ROOT / "tools" / "sweep_auto.py"),
           "--seen", args.seen, "--tracker", args.tracker,
           "--state", args.state, "--artifacts", args.artifacts,
           "run", "--date", run_date]
    proc = subprocess.run(cmd, cwd=ROOT)
    if proc.returncode != 0:
        print(f"sweep-auto: tools/sweep_auto.py run failed (exit {proc.returncode})",
              file=sys.stderr)
        return proc.returncode
    art_dir = Path(args.artifacts)
    if not art_dir.is_absolute():
        art_dir = ROOT / art_dir  # the sweep ran with cwd=ROOT
    artifact = art_dir / f"sweep-{run_date}.md"
    if not artifact.exists():
        print(f"sweep-auto: sweep exited 0 but {artifact} is missing", file=sys.stderr)
        return 1
    try:
        shown = artifact.relative_to(ROOT)
    except ValueError:  # --artifacts outside the repo: print the absolute path
        shown = artifact
    print(f"artifact={shown}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
