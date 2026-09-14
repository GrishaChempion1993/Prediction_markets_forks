import { fetchJson, toSnapshot } from "../utils.js";
import type { MarketSnapshotV1 } from "../types.js";

const SXBET_API_BASE = process.env.SXBET_API_BASE ?? "https://api.sx.bet";
const SXBET_ACTIVE_MARKETS_PATH = process.env.SXBET_ACTIVE_MARKETS_PATH ?? "/markets/active";
const SXBET_ORDERS_PATH = process.env.SXBET_ORDERS_PATH ?? "/orders";

interface SxBetMarketResponse {
  status?: string;
  data?: {
    markets?: Array<Record<string, unknown>>;
    nextKey?: string | null;
  };
}

interface SxBetOrdersResponse {
  status?: string;
  data?: Array<Record<string, unknown>>;
}

export async function collectSxBet(maxMarkets: number): Promise<MarketSnapshotV1[]> {
  const markets: Array<Record<string, unknown>> = [];
  let nextKey: string | null = null;

  while (markets.length < maxMarkets) {
    const params = new URLSearchParams();
    if (nextKey) {
      params.set("nextKey", nextKey);
    }
    const url = `${SXBET_API_BASE}${SXBET_ACTIVE_MARKETS_PATH}${params.size ? `?${params.toString()}` : ""}`;
    const payload = await fetchJson<SxBetMarketResponse>(url);
    const batch = payload.data?.markets ?? [];
    if (batch.length === 0) {
      break;
    }
    markets.push(...batch);
    nextKey = payload.data?.nextKey ?? null;
    if (!nextKey) {
      break;
    }
  }

  let orders: Array<Record<string, unknown>> = [];
  try {
    const payload = await fetchJson<SxBetOrdersResponse>(`${SXBET_API_BASE}${SXBET_ORDERS_PATH}`);
    orders = payload.data ?? [];
  } catch (error) {
    orders = [
      {
        error: error instanceof Error ? error.message : "sxbet_orders_fetch_failed",
      },
    ];
  }

  const orderMap = new Map<string, Array<Record<string, unknown>>>();
  for (const order of orders) {
    const marketHash = String(order.marketHash ?? "");
    if (!marketHash) {
      continue;
    }
    const grouped = orderMap.get(marketHash) ?? [];
    grouped.push(order);
    orderMap.set(marketHash, grouped);
  }

  return markets.slice(0, maxMarkets).map((market) => {
    const marketHash = String(market.marketHash ?? market.id);
    return toSnapshot(
      "sxbet",
      marketHash,
      {
        collector: "ts.sxbet.public_api",
        variant: "public_api",
        status: "ok",
        completeness: 1,
        endpoints: [`${SXBET_API_BASE}${SXBET_ACTIVE_MARKETS_PATH}`, `${SXBET_API_BASE}${SXBET_ORDERS_PATH}`],
        notes: ["Active markets plus public orders snapshot for quote estimation."],
      },
      {
        ...market,
        orders: orderMap.get(marketHash) ?? [],
      },
    );
  });
}
