import { createGzip } from "node:zlib";
import { execFile } from "node:child_process";
import { createWriteStream, mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { pipeline } from "node:stream/promises";
import { Readable } from "node:stream";
import { promisify } from "node:util";

import type { CollectorArgs, CollectorSource, CollectorSourceStatus, Exchange, MarketSnapshotV1 } from "./types.js";

export interface CollectorSourceInput {
  collector: string;
  endpoints: string[];
  variant?: string;
  status?: CollectorSourceStatus;
  completeness?: number;
  notes?: string[];
}

const DEFAULT_MAX_MARKETS = 50;
const SUPPORTED_EXCHANGES: Exchange[] = ["polymarket", "opn", "azuro", "sxbet", "myriad", "kalshi"];
const execFileAsync = promisify(execFile);
const CURL_BINARY = process.platform === "win32" ? "curl.exe" : "curl";
const REQUEST_RETRY_DELAYS_MS = [0, 1000, 3000, 6000];
const REQUEST_TIMEOUT_MS = Number.parseInt(process.env.HTTP_REQUEST_TIMEOUT_MS ?? "30000", 10);
const CURL_TIMEOUT_SECONDS = Math.max(1, Math.ceil(REQUEST_TIMEOUT_MS / 1000));
const CURL_MAX_BUFFER_BYTES = Number.parseInt(process.env.CURL_MAX_BUFFER_BYTES ?? `${64 * 1024 * 1024}`, 10);
const REQUEST_TRANSPORT = (process.env.HTTP_TRANSPORT ?? process.env.REQUEST_TRANSPORT ?? "").trim().toLowerCase();
const USE_HTTP_PROXY =
  process.env.USE_HTTP_PROXY === "1" ||
  process.env.USE_HTTP_PROXY === "true" ||
  process.env.USE_HTTP_PROXY === "yes" ||
  process.env.USE_HTTP_PROXY === "on";
const HTTP_PROXY_POOL = (process.env.HTTP_PROXY_POOL ?? "")
  .split(",")
  .map((value) => value.trim())
  .filter(Boolean);
const HTTPS_PROXY_POOL = (process.env.HTTPS_PROXY_POOL ?? "")
  .split(",")
  .map((value) => value.trim())
  .filter(Boolean);
const FORCE_CURL =
  REQUEST_TRANSPORT === "curl" ||
  (REQUEST_TRANSPORT !== "fetch" && (process.env.FORCE_CURL === "1" || process.env.FORCE_CURL === "true"));

export function parseArgs(argv: string[]): CollectorArgs {
  const args = new Map<string, string>();
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (!token.startsWith("--")) {
      continue;
    }
    const key = token.slice(2);
    const value = argv[index + 1] && !argv[index + 1].startsWith("--") ? argv[index + 1] : "true";
    args.set(key, value);
  }

  const exchange = args.get("exchange") as Exchange | undefined;
  if (!exchange || !SUPPORTED_EXCHANGES.includes(exchange)) {
    throw new Error(`Expected --exchange ${SUPPORTED_EXCHANGES.join("|")}`);
  }

  const maxMarketsRaw = args.get("max-markets") ?? process.env.MAX_MARKETS ?? `${DEFAULT_MAX_MARKETS}`;
  const maxMarkets = Number.parseInt(maxMarketsRaw, 10);
  if (!Number.isFinite(maxMarkets) || maxMarkets <= 0) {
    throw new Error("Expected positive integer for --max-markets");
  }

  return {
    exchange,
    out: args.get("out"),
    maxMarkets,
  };
}

export function defaultOutPath(exchange: Exchange): string {
  const timestamp = new Date().toISOString().replaceAll(":", "-");
  return `data/ingest/${exchange}/${timestamp}.jsonl.gz`;
}

export async function writeSnapshotsGzip(path: string, snapshots: MarketSnapshotV1[]): Promise<void> {
  mkdirSync(dirname(path), { recursive: true });
  const content = snapshots.map((snapshot) => `${JSON.stringify(snapshot)}\n`).join("");
  await pipeline(Readable.from([content]), createGzip(), createWriteStream(path));
}

function normalizeHeaders(headers?: HeadersInit): Record<string, string> {
  if (!headers) {
    return {};
  }
  if (Array.isArray(headers)) {
    return Object.fromEntries(headers.map(([key, value]) => [key, value]));
  }
  if (headers instanceof Headers) {
    return Object.fromEntries(headers.entries());
  }
  return headers;
}

function selectProxy(url: string, attemptIndex: number): string | null {
  if (!USE_HTTP_PROXY) {
    return null;
  }
  const isHttps = url.toLowerCase().startsWith("https://");
  const pool = isHttps ? HTTPS_PROXY_POOL : HTTP_PROXY_POOL;
  if (pool.length === 0) {
    return null;
  }
  return pool[attemptIndex % pool.length] ?? null;
}

async function curlRequest(url: string, init?: RequestInit, proxyUrl?: string | null): Promise<string> {
  const args = [
    "-L",
    "-sS",
    "--fail-with-body",
    "--connect-timeout",
    String(Math.min(CURL_TIMEOUT_SECONDS, 10)),
    "--max-time",
    String(CURL_TIMEOUT_SECONDS),
  ];
  if (proxyUrl) {
    args.push("--proxy", proxyUrl);
  }
  const method = init?.method ?? "GET";
  if (method !== "GET") {
    args.push("-X", method);
  }
  const headers = {
    "user-agent": "prediction-arb-scanner/0.1",
    ...normalizeHeaders(init?.headers),
  };
  for (const [key, value] of Object.entries(headers)) {
    args.push("-H", `${key}: ${value}`);
  }
  if (typeof init?.body === "string") {
    args.push("--data", init.body);
  }
  args.push(url);
  const options =
    process.platform === "win32"
      ? { windowsHide: true, maxBuffer: CURL_MAX_BUFFER_BYTES }
      : { maxBuffer: CURL_MAX_BUFFER_BYTES };
  const { stdout } = await execFileAsync(CURL_BINARY, args, options);
  return stdout;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

function requestSignal(init?: RequestInit): AbortSignal | undefined {
  const timeoutSignal = REQUEST_TIMEOUT_MS > 0 ? AbortSignal.timeout(REQUEST_TIMEOUT_MS) : undefined;
  if (timeoutSignal && init?.signal) {
    return AbortSignal.any([init.signal, timeoutSignal]);
  }
  return init?.signal ?? timeoutSignal;
}

async function curlRequestWithFallback(url: string, init?: RequestInit, attemptIndex = 0): Promise<string> {
  let directError: unknown;
  try {
    return await curlRequest(url, init);
  } catch (error) {
    directError = error;
  }

  const proxyUrl = selectProxy(url, attemptIndex);
  if (!proxyUrl) {
    throw directError instanceof Error ? directError : new Error(`Request failed for ${url}`);
  }
  return curlRequest(url, init, proxyUrl);
}

async function fetchTextOnce(url: string, init?: RequestInit, attemptIndex = 0): Promise<string> {
  if (FORCE_CURL) {
    return curlRequestWithFallback(url, init, attemptIndex);
  }
  try {
    const response = await fetch(url, {
      ...init,
      signal: requestSignal(init),
      headers: {
        "user-agent": "prediction-arb-scanner/0.1",
        ...(normalizeHeaders(init?.headers) ?? {}),
      },
    });
    if (!response.ok) {
      throw new Error(`Request failed ${response.status} for ${url}`);
    }
    return response.text();
  } catch {
    return curlRequestWithFallback(url, init, attemptIndex);
  }
}

async function fetchTextWithRetry(url: string, init?: RequestInit): Promise<string> {
  let lastError: unknown;
  for (const [attemptIndex, delayMs] of REQUEST_RETRY_DELAYS_MS.entries()) {
    if (delayMs > 0) {
      await sleep(delayMs);
    }
    try {
      return await fetchTextOnce(url, init, attemptIndex);
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError instanceof Error ? lastError : new Error(`Request failed for ${url}`);
}

export async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  return JSON.parse(await fetchTextWithRetry(url, init)) as T;
}

export async function fetchText(url: string, init?: RequestInit): Promise<string> {
  return fetchTextWithRetry(url, init);
}

export function toSnapshot(
  exchange: Exchange,
  marketId: string,
  source: CollectorSourceInput,
  raw: Record<string, unknown>,
): MarketSnapshotV1 {
  return {
    schema_version: "market_snapshot.v1",
    exchange,
    market_id: marketId,
    fetched_at: new Date().toISOString(),
    source: normalizeCollectorSource(source),
    raw,
  };
}

export function normalizeCollectorSource(source: CollectorSourceInput): CollectorSource {
  const completeness = typeof source.completeness === "number" && Number.isFinite(source.completeness)
    ? source.completeness
    : 1.0;
  return {
    collector: source.collector,
    variant: source.variant?.trim() || inferCollectorVariant(source.collector),
    status: source.status ?? "ok",
    completeness: Math.min(1, Math.max(0, completeness)),
    endpoints: source.endpoints,
    notes: source.notes,
  };
}

function inferCollectorVariant(collector: string): string {
  const parts = collector.split(".").map((value) => value.trim()).filter(Boolean);
  return parts[parts.length - 1] || "default";
}

export function chunk<T>(items: T[], size: number): T[][];
export function chunk<T>(items: T[], size: number): T[][] {
  const chunks: T[][] = [];
  for (let index = 0; index < items.length; index += size) {
    chunks.push(items.slice(index, index + size));
  }
  return chunks;
}

export function safeJsonParse<T>(value: string | undefined, fallback: T): T {
  if (!value) {
    return fallback;
  }
  try {
    return JSON.parse(value) as T;
  } catch {
    return fallback;
  }
}

export function uniqueBy<T>(items: T[], keyFn: (item: T) => string): T[] {
  const seen = new Set<string>();
  const unique: T[] = [];
  for (const item of items) {
    const key = keyFn(item);
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    unique.push(item);
  }
  return unique;
}
