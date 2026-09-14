import { fetchJson, toSnapshot, uniqueBy } from "../utils.js";
import type { MarketSnapshotV1 } from "../types.js";

const KALSHI_API_BASE = process.env.KALSHI_API_BASE ?? "https://api.elections.kalshi.com/trade-api/v2";
const KALSHI_EVENTS_PATH = process.env.KALSHI_EVENTS_PATH ?? "/events";
const KALSHI_MARKETS_PATH = process.env.KALSHI_MARKETS_PATH ?? "/markets";
const KALSHI_PAGE_SIZE = Number.parseInt(process.env.KALSHI_PAGE_SIZE ?? "1000", 10);
const KALSHI_STATUS = process.env.KALSHI_STATUS ?? "open";
const KALSHI_INCLUDE_COMPLEX = (process.env.KALSHI_INCLUDE_COMPLEX ?? "false").trim().toLowerCase() === "true";
const KALSHI_EVENT_CATEGORY_FILTER = (process.env.KALSHI_EVENT_CATEGORY_FILTER ?? "").trim().toLowerCase();
const KALSHI_MAX_EVENT_PAGES = Number.parseInt(process.env.KALSHI_MAX_EVENT_PAGES ?? "25", 10);

interface KalshiMarketResponse {
  cursor?: string | null;
  markets?: Array<Record<string, unknown>>;
}

interface KalshiEventResponse {
  cursor?: string | null;
  events?: Array<Record<string, unknown>>;
}

function isPlainKalshiMarket(market: Record<string, unknown>): boolean {
  if (KALSHI_INCLUDE_COMPLEX) {
    return true;
  }
  if (typeof market.mve_collection_ticker === "string" && market.mve_collection_ticker.length > 0) {
    return false;
  }
  const customStrike = market.custom_strike;
  if (customStrike && typeof customStrike === "object") {
    const keys = Object.keys(customStrike as Record<string, unknown>);
    if (keys.some((key) => key.startsWith("Associated "))) {
      return false;
    }
  }
  return true;
}

async function fetchKalshiJson(url: string): Promise<KalshiMarketResponse> {
  return fetchJson<KalshiMarketResponse>(url);
}

async function collectKalshiFromEvents(maxMarkets: number): Promise<MarketSnapshotV1[]> {
  const markets: Array<Record<string, unknown>> = [];
  let cursor: string | null = null;
  const eventLimit = String(Math.min(Math.max(KALSHI_PAGE_SIZE, maxMarkets), 200));
  let pageCount = 0;

  while (markets.length < maxMarkets && pageCount < KALSHI_MAX_EVENT_PAGES) {
    pageCount += 1;
    const params = new URLSearchParams({
      limit: eventLimit,
      status: KALSHI_STATUS,
      with_nested_markets: "true",
    });
    if (cursor) {
      params.set("cursor", cursor);
    }

    const url = `${KALSHI_API_BASE}${KALSHI_EVENTS_PATH}?${params.toString()}`;
    const payload = await fetchJson<KalshiEventResponse>(url);
    const events = payload.events ?? [];
    if (events.length === 0) {
      break;
    }

    for (const event of events) {
      const eventCategory = String(event.category ?? "").trim().toLowerCase();
      if (KALSHI_EVENT_CATEGORY_FILTER && eventCategory !== KALSHI_EVENT_CATEGORY_FILTER) {
        continue;
      }
      const nestedMarkets = Array.isArray(event.markets) ? event.markets : [];
      for (const market of nestedMarkets) {
        if (!market || typeof market !== "object" || !isPlainKalshiMarket(market as Record<string, unknown>)) {
          continue;
        }
        markets.push({
          ...market,
          event_title: event.title,
          event_sub_title: event.sub_title,
          event_category: event.category,
          event_series_ticker: event.series_ticker,
          event_strike_date: event.strike_date,
          event_last_updated_ts: event.last_updated_ts,
        });
        if (markets.length >= maxMarkets) {
          break;
        }
      }
      if (markets.length >= maxMarkets) {
        break;
      }
    }

    cursor = payload.cursor ?? null;
    if (!cursor) {
      break;
    }
  }

  return uniqueBy(markets, (market) => String(market.ticker ?? market.market_ticker ?? JSON.stringify(market)))
    .slice(0, maxMarkets)
    .map((market) =>
      toSnapshot(
        "kalshi",
        String(market.ticker ?? market.market_ticker),
        {
          collector: "ts.kalshi.events_v2",
          variant: "events_nested",
          status: "degraded",
          completeness: 0.9,
          endpoints: [`${KALSHI_API_BASE}${KALSHI_EVENTS_PATH}`],
          notes: [
            "Kalshi events API with nested markets. Multivariate events are excluded upstream by the endpoint.",
            ...(KALSHI_EVENT_CATEGORY_FILTER ? [`event.category=${KALSHI_EVENT_CATEGORY_FILTER}`] : []),
          ],
        },
        market,
      ),
    );
}

async function collectKalshiFromMarkets(maxMarkets: number): Promise<MarketSnapshotV1[]> {
  const markets: Array<Record<string, unknown>> = [];
  let cursor: string | null = null;
  const requestLimit = String(Math.min(Math.max(KALSHI_PAGE_SIZE, maxMarkets), 1000));
  let truncatedReason: string | null = null;

  while (markets.length < maxMarkets) {
    const params = new URLSearchParams({
      limit: requestLimit,
      status: KALSHI_STATUS,
    });
    if (cursor) {
      params.set("cursor", cursor);
    }

    const url = `${KALSHI_API_BASE}${KALSHI_MARKETS_PATH}?${params.toString()}`;
    let payload: KalshiMarketResponse;
    try {
      payload = await fetchKalshiJson(url);
    } catch (error) {
      if (markets.length === 0) {
        throw error;
      }
      truncatedReason = error instanceof Error ? error.message : String(error);
      break;
    }
    const rawBatch = payload.markets ?? [];
    if (rawBatch.length === 0) {
      break;
    }
    const batch = rawBatch.filter(isPlainKalshiMarket);
    markets.push(...batch);
    cursor = payload.cursor ?? null;
    if (!cursor) {
      break;
    }
  }

  return uniqueBy(markets, (market) => String(market.ticker ?? market.market_ticker ?? JSON.stringify(market)))
    .slice(0, maxMarkets)
    .map((market) =>
      toSnapshot(
        "kalshi",
        String(market.ticker ?? market.market_ticker),
        {
          collector: "ts.kalshi.trade_api_v2",
          variant: "markets",
          status: truncatedReason ? "partial" : "ok",
          completeness: truncatedReason ? 0.85 : 1,
          endpoints: [`${KALSHI_API_BASE}${KALSHI_MARKETS_PATH}`],
          notes: truncatedReason
            ? ["Public Kalshi trade API v2 market snapshot.", `Collection truncated: ${truncatedReason}`]
            : ["Public Kalshi trade API v2 market snapshot."],
        },
        market,
      ),
    );
}

export async function collectKalshi(maxMarkets: number): Promise<MarketSnapshotV1[]> {
  try {
    const snapshots = await collectKalshiFromMarkets(maxMarkets);
    if (snapshots.length > 0) {
      return snapshots;
    }
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    process.stderr.write(`kalshi_markets_collect_failed fallback=events error=${message}\n`);
  }
  return collectKalshiFromEvents(maxMarkets);
}
