import { fetchJson, safeJsonParse, toSnapshot } from "../utils.js";
import type { MarketSnapshotV1 } from "../types.js";

const GAMMA_BASE = process.env.POLYMARKET_GAMMA_BASE ?? "https://gamma-api.polymarket.com";
const CLOB_BASE = process.env.POLYMARKET_CLOB_BASE ?? "https://clob.polymarket.com";
const PAGE_SIZE = 50;
const BOOK_LIMIT = Number.parseInt(process.env.POLYMARKET_BOOK_LIMIT ?? "25", 10);

interface PolymarketMarket {
  id: string | number;
  question?: string;
  description?: string;
  outcomes?: string;
  outcomePrices?: string;
  clobTokenIds?: string;
  enableOrderBook?: boolean;
  [key: string]: unknown;
}

export async function collectPolymarket(maxMarkets: number): Promise<MarketSnapshotV1[]> {
  const markets: PolymarketMarket[] = [];
  let offset = 0;

  while (markets.length < maxMarkets) {
    const url =
      `${GAMMA_BASE}/markets?active=true&closed=false&limit=${PAGE_SIZE}&offset=${offset}`;
    const page = await fetchJson<PolymarketMarket[]>(url);
    if (page.length === 0) {
      break;
    }
    markets.push(...page);
    if (page.length < PAGE_SIZE) {
      break;
    }
    offset += PAGE_SIZE;
  }

  const snapshots: MarketSnapshotV1[] = [];
  for (const [index, market] of markets.slice(0, maxMarkets).entries()) {
    const clobTokenIds = safeJsonParse<string[]>(typeof market.clobTokenIds === "string" ? market.clobTokenIds : undefined, []);
    const orderBooks: Record<string, unknown> = {};

    if (market.enableOrderBook && index < BOOK_LIMIT) {
      for (const tokenId of clobTokenIds) {
        try {
          const book = await fetchJson<Record<string, unknown>>(`${CLOB_BASE}/book?token_id=${tokenId}`);
          orderBooks[tokenId] = book;
        } catch (error) {
          orderBooks[tokenId] = {
            error: error instanceof Error ? error.message : "unknown_orderbook_error",
          };
        }
      }
    }

    snapshots.push(
      toSnapshot(
        "polymarket",
        String(market.id),
        {
          collector: "ts.polymarket.gamma",
          variant: "gamma_clob",
          status: "ok",
          completeness: 1,
          endpoints: [`${GAMMA_BASE}/markets`, `${CLOB_BASE}/book`],
          notes: ["Gamma market metadata plus optional public CLOB books."],
        },
        {
          ...market,
          clobTokenIds,
          orderBooks,
        },
      ),
    );
  }

  return snapshots;
}
