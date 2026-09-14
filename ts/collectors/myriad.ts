import { fetchText, toSnapshot, uniqueBy } from "../utils.js";
import type { MarketSnapshotV1 } from "../types.js";

const MYRIAD_MARKETS_URL = process.env.MYRIAD_MARKETS_URL ?? "https://myriad.markets/markets";

function extractRemixContext(html: string): Record<string, unknown> {
  const match = html.match(/window\.__remixContext = (\{.*?\});<\/script>/s);
  if (!match) {
    throw new Error("Unable to locate window.__remixContext bootstrap payload");
  }
  return JSON.parse(match[1]) as Record<string, unknown>;
}

function findMarketArrays(value: unknown, found: Array<Record<string, unknown>>[]): void {
  if (Array.isArray(value)) {
    if (value.length > 0 && value.every((item) => typeof item === "object" && item !== null && "id" in item && "outcomes" in item)) {
      found.push(value as Array<Record<string, unknown>>);
      return;
    }
    for (const item of value) {
      findMarketArrays(item, found);
    }
    return;
  }

  if (value && typeof value === "object") {
    for (const nested of Object.values(value)) {
      findMarketArrays(nested, found);
    }
  }
}

export async function collectMyriad(maxMarkets: number): Promise<MarketSnapshotV1[]> {
  const html = await fetchText(MYRIAD_MARKETS_URL);
  const remixContext = extractRemixContext(html);
  const arrays: Array<Record<string, unknown>>[] = [];
  findMarketArrays(remixContext, arrays);

  const markets = uniqueBy(arrays.flat(), (item) => String(item.id)).slice(0, maxMarkets);

  return markets.map((market) =>
    toSnapshot(
      "myriad",
      String(market.id),
      {
        collector: "ts.myriad.remix_bootstrap",
        variant: "remix_bootstrap",
        status: "degraded",
        completeness: 0.8,
        endpoints: [MYRIAD_MARKETS_URL],
        notes: ["Experimental parser over public Remix bootstrap state."],
      },
      market,
    ),
  );
}
