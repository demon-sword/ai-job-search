import { join } from "path";

const CLI_PATH = join(import.meta.dir, "../src/cli.ts");

export interface CLIResult {
  stdout: string;
  stderr: string;
  exitCode: number;
}

export async function runCLI(args: string[], opts?: { cwd?: string; env?: Record<string, string> }): Promise<CLIResult> {
  const env: Record<string, string> = { ...(opts?.env ?? process.env as Record<string, string>) };
  delete env.CHROME_DEVTOOLS_AXI_BROWSER_URL;
  delete env.CHROME_DEVTOOLS_AXI_USER_DATA_DIR;
  const proc = Bun.spawn(["bun", "run", CLI_PATH, ...args], {
    stdout: "pipe",
    stderr: "pipe",
    cwd: opts?.cwd,
    env,
  });

  const [stdout, stderr, exitCode] = await Promise.all([
    new Response(proc.stdout).text(),
    new Response(proc.stderr).text(),
    proc.exited,
  ]);

  return { stdout: stdout.trim(), stderr: stderr.trim(), exitCode };
}

export function parseJSON<T = unknown>(result: CLIResult): T {
  if (result.exitCode !== 0) {
    throw new Error(`CLI exited with code ${result.exitCode}. stderr: ${result.stderr}`);
  }
  try {
    return JSON.parse(result.stdout) as T;
  } catch {
    throw new Error(`Failed to parse JSON. stdout: ${result.stdout}\nstderr: ${result.stderr}`);
  }
}

export function parseStderr(result: CLIResult): { error: string; code: string } {
  return JSON.parse(result.stderr) as { error: string; code: string };
}
