export type Exchange = "polymarket" | "opn" | "azuro" | "sxbet" | "myriad" | "kalshi";

export type CollectorSourceStatus = "ok" | "partial" | "degraded" | "error";

export interface CollectorSource {
  collector: string;
  variant: string;
  status: CollectorSourceStatus;
  completeness: number;
  endpoints: string[];
  notes?: string[];
}

export interface MarketSnapshotV1 {
  schema_version: "market_snapshot.v1";
  exchange: Exchange;
  market_id: string;
  fetched_at: string;
  source: CollectorSource;
  raw: Record<string, unknown>;
}

export interface CollectorArgs {
  exchange: Exchange;
  out?: string;
  maxMarkets: number;
}
