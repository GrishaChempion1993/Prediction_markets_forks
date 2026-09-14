import { fetchJson, toSnapshot, uniqueBy } from "../utils.js";
import type { MarketSnapshotV1 } from "../types.js";

const OPN_API_BASE = process.env.OPN_API_BASE ?? "https://openapi.opinion.trade/openapi";
const OPN_MARKETS_PATH = process.env.OPN_MARKETS_PATH ?? "/market";
const OPN_PAGE_SIZE = Number.parseInt(process.env.OPN_PAGE_SIZE ?? "20", 10);
const OPN_API_KEY = process.env.OPN_API_KEY ?? "";
const OPN_SOURCE_MODE = (process.env.OPN_SOURCE_MODE ?? "auto").trim().toLowerCase();
const OPN_WEB_BASE = process.env.OPN_WEB_BASE ?? "https://proxy.opinion.trade:8443";
const OPN_WEB_TOPIC_PATH = process.env.OPN_WEB_TOPIC_PATH ?? "/api/bsc/api/v2/topic";
const OPN_WEB_PAGE_SIZE = Number.parseInt(process.env.OPN_WEB_PAGE_SIZE ?? "20", 10);
const OPN_WEB_CHAIN_ID = process.env.OPN_WEB_CHAIN_ID ?? "56";
const OPN_WEB_SORT_BY = process.env.OPN_WEB_SORT_BY ?? "1";

const OPN_WEB_TOPIC_STATUS = "2";
const OPN_WEB_IS_SHOW = "1";
const OPN_WEB_TOPIC_TYPE = "2";
const OPN_WEB_INDICATOR_TYPE = "0";
const OPN_WEB_EXCLUDE_PIN = "1";

type OpnMarket = Record<string, unknown>;

interface OpnMarketResponse {
  data?: OpnMarket[];
  markets?: OpnMarket[];
  items?: OpnMarket[];
  result?: {
    list?: OpnMarket[];
    items?: OpnMarket[];
    total?: number;
  };
  nextPage?: string | number | null;
  hasMore?: boolean;
}

interface OpnWebTopicResponse {
  result?: {
    list?: OpnMarket[];
    total?: number;
  };
}

function authHeaders(): Record<string, string> {
  if (!OPN_API_KEY) {
    return {};
  }
  return {
    apikey: OPN_API_KEY,
    "x-api-key": OPN_API_KEY,
    authorization: `Bearer ${OPN_API_KEY}`,
  };
}

function extractItems(payload: OpnMarketResponse | OpnMarket[]): OpnMarket[] {
  if (Array.isArray(payload)) {
    return payload;
  }
  if (Array.isArray(payload.data)) {
    return payload.data;
  }
  if (Array.isArray(payload.markets)) {
    return payload.markets;
  }
  if (Array.isArray(payload.items)) {
    return payload.items;
  }
  if (Array.isArray(payload.result?.list)) {
    return payload.result.list;
  }
  if (Array.isArray(payload.result?.items)) {
    return payload.result.items;
  }
  return [];
}

function sourceMode(): "api" | "web" | "auto" {
  if (OPN_SOURCE_MODE === "api" || OPN_SOURCE_MODE === "web") {
    return OPN_SOURCE_MODE;
  }
  return "auto";
}

function webHeaders(): Record<string, string> {
  return {
    accept: "application/json, text/plain, */*",
    origin: "https://app.opinion.trade",
    referer: "https://app.opinion.trade/trending",
  };
}

async function collectOpnFromApi(maxMarkets: number): Promise<MarketSnapshotV1[]> {
  const markets: OpnMarket[] = [];
  let page = 1;

  while (markets.length < maxMarkets) {
    const params = new URLSearchParams({
      page: String(page),
      limit: String(OPN_PAGE_SIZE),
      status: process.env.OPN_MARKET_STATUS ?? "activated",
    });
    const url = `${OPN_API_BASE}${OPN_MARKETS_PATH}?${params.toString()}`;
    const payload = await fetchJson<OpnMarketResponse | OpnMarket[]>(url, {
      headers: authHeaders(),
    });
    const items = extractItems(payload);
    if (items.length === 0) {
      break;
    }
    markets.push(...items);
    if (items.length < OPN_PAGE_SIZE) {
      break;
    }
    page += 1;
  }

  return uniqueBy(markets, (market) => String(market.id ?? market.marketId ?? market.slug ?? JSON.stringify(market)))
    .slice(0, maxMarkets)
    .map((market) =>
      toSnapshot(
        "opn",
        String(market.id ?? market.marketId ?? market.slug),
        {
          collector: "ts.opn.rest_api",
          variant: "openapi",
          status: "ok",
          completeness: 1,
          endpoints: [`${OPN_API_BASE}${OPN_MARKETS_PATH}`],
          notes: OPN_API_KEY ? ["Authenticated with OPN_API_KEY."] : ["Public market discovery without API key."],
        },
        market,
      ),
    );
}

async function collectOpnFromWeb(maxMarkets: number): Promise<MarketSnapshotV1[]> {
  const topics: OpnMarket[] = [];
  let page = 1;

  while (topics.length < maxMarkets) {
    const params = new URLSearchParams({
      page: String(page),
      limit: String(Math.min(OPN_WEB_PAGE_SIZE, maxMarkets)),
      sortBy: OPN_WEB_SORT_BY,
      chainId: OPN_WEB_CHAIN_ID,
      status: OPN_WEB_TOPIC_STATUS,
      isShow: OPN_WEB_IS_SHOW,
      topicType: OPN_WEB_TOPIC_TYPE,
      indicatorType: OPN_WEB_INDICATOR_TYPE,
      excludePin: OPN_WEB_EXCLUDE_PIN,
    });
    const url = `${OPN_WEB_BASE}${OPN_WEB_TOPIC_PATH}?${params.toString()}`;
    const payload = await fetchJson<OpnWebTopicResponse>(url, {
      headers: webHeaders(),
    });
    const items = payload.result?.list ?? [];
    if (items.length === 0) {
      break;
    }
    topics.push(...items);
    if (items.length < OPN_WEB_PAGE_SIZE) {
      break;
    }
    page += 1;
  }

  return uniqueBy(topics, (market) => String(market.slug ?? market.topicId ?? market.id ?? JSON.stringify(market)))
    .slice(0, maxMarkets)
    .map((market) =>
      toSnapshot(
        "opn",
        String(market.slug ?? market.topicId ?? market.id),
        {
          collector: "ts.opn.web_topic_api",
          variant: "web_topic_api",
          status: "degraded",
          completeness: 0.9,
          endpoints: [`${OPN_WEB_BASE}${OPN_WEB_TOPIC_PATH}`],
          notes: ["Public OPINION web backend used as API-key-free fallback."],
        },
        market,
      ),
    );
}

export async function collectOpn(maxMarkets: number): Promise<MarketSnapshotV1[]> {
  const mode = sourceMode();
  if (mode === "api") {
    return collectOpnFromApi(maxMarkets);
  }
  if (mode === "web") {
    return collectOpnFromWeb(maxMarkets);
  }

  if (OPN_API_KEY) {
    try {
      return await collectOpnFromApi(maxMarkets);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      process.stderr.write(`opn_api_collect_failed fallback=web error=${message}\n`);
    }
  }

  return collectOpnFromWeb(maxMarkets);
}
