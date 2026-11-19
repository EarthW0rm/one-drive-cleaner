// SPDX-License-Identifier: GPL-3.0-only
import { spawn, type ChildProcess } from "node:child_process";
import { fileURLToPath } from "node:url";

const REPO_ROOT = fileURLToPath(new URL("../..", import.meta.url));
const START_TIMEOUT_MS = 90_000;
const STOP_TIMEOUT_MS = 20_000;
const STDERR_TAIL_BYTES = 20_000;
const READY = /^\{"app_url": "(?<app>[^"]+)", "graph_url": "(?<graph>[^"]+)"\}$/;

export interface Stack {
  readonly appUrl: string;
  readonly graphUrl: string;
  /** Fim do stderr do launcher (logs do `odc`), para diagnóstico de falhas. */
  readonly log: () => string;
  /** Encerra o launcher (grupo de processos) e aguarda a saída. */
  readonly stop: () => Promise<void>;
}

function exited(child: ChildProcess): Promise<void> {
  return new Promise((resolve) => {
    if (child.exitCode !== null || child.signalCode !== null) {
      resolve();
      return;
    }
    child.once("exit", () => {
      resolve();
    });
  });
}

function signalGroup(child: ChildProcess, signal: NodeJS.Signals): void {
  if (child.pid === undefined) {
    return;
  }
  try {
    // `detached` faz do launcher líder do grupo: o sinal alcança também o `uv` e o Python.
    process.kill(-child.pid, signal);
  } catch {
    // grupo já encerrado
  }
}

async function stopGroup(child: ChildProcess): Promise<void> {
  signalGroup(child, "SIGTERM");
  const timer = setTimeout(() => {
    signalGroup(child, "SIGKILL");
  }, STOP_TIMEOUT_MS);
  try {
    await exited(child);
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Sobe uma instância isolada do ambiente (Graph fake + `odc` real, banco e diretórios
 * temporários próprios) pelo launcher de `tests/e2e_launcher`. Cada teste usa a sua: estado,
 * modelo e Graph fake nunca vazam entre cenários nem entre navegadores.
 */
export async function startStack(): Promise<Stack> {
  const child = spawn("uv", ["run", "python", "-m", "tests.e2e_launcher.launch"], {
    cwd: REPO_ROOT,
    detached: true,
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stderrTail = "";
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk: string) => {
    stderrTail = (stderrTail + chunk).slice(-STDERR_TAIL_BYTES);
  });

  const urls = await new Promise<{ app: string; graph: string }>((resolve, reject) => {
    let buffer = "";
    const timer = setTimeout(() => {
      reject(new Error(`o launcher E2E não iniciou em ${String(START_TIMEOUT_MS)} ms`));
    }, START_TIMEOUT_MS);
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => {
      buffer += chunk;
      for (const line of buffer.split("\n")) {
        const groups = READY.exec(line.trim())?.groups;
        if (groups?.app !== undefined && groups.graph !== undefined) {
          clearTimeout(timer);
          resolve({ app: groups.app, graph: groups.graph });
          return;
        }
      }
    });
    child.once("exit", (code) => {
      clearTimeout(timer);
      reject(new Error(`o launcher E2E terminou (código ${String(code)})\n${stderrTail}`));
    });
  }).catch(async (error: unknown) => {
    await stopGroup(child);
    throw error;
  });
  // Depois da linha de prontidão o stdout continua sendo drenado (evita bloquear o filho).
  child.stdout.resume();

  return {
    appUrl: urls.app,
    graphUrl: urls.graph,
    log: () => stderrTail,
    stop: () => stopGroup(child),
  };
}
