import { chunk, fetchJson, toSnapshot } from "../utils.js";
import type { MarketSnapshotV1 } from "../types.js";

const AZURO_API_BASE = process.env.AZURO_API_BASE ?? "https://api.onchainfeed.org";
const DEFAULT_ENVIRONMENTS = (process.env.AZURO_ENVIRONMENTS ?? "PolygonUSDT,GnosisXDAI,BaseWETH")
  .split(",")
  .map((value: string) => value.trim())
  .filter(Boolean);

interface AzuroGame {
  id?: string;
  gameId: string;
  title: string;
  startsAt?: string;
  turnover?: string;
  sport?: { name?: string; slug?: string; sportId?: string };
  league?: { name?: string; slug?: string };
  country?: { name?: string; slug?: string };
  participants?: Array<{ name?: string }>;
  [key: string]: unknown;
}

interface AzuroGameResponse {
  games: AzuroGame[];
}

interface AzuroConditionResponse {
  conditions: Array<Record<string, unknown>>;
}

async function fetchGames(environment: string, page: number): Promise<AzuroGame[]> {
  const params = new URLSearchParams({
    gameState: "Prematch",
    environment,
    orderBy: "startsAt",
    orderDirection: "asc",
    page: String(page),
    perPage: "20",
  });
  const url = `${AZURO_API_BASE}/api/v1/public/market-manager/games-by-filters?${params.toString()}`;
  const data = await fetchJson<AzuroGameResponse>(url);
  return data.games ?? [];
}

async function fetchConditions(environment: string, gameIds: string[]): Promise<Array<Record<string, unknown>>> {
  const url = `${AZURO_API_BASE}/api/v1/public/market-manager/conditions-by-game-ids`;
  const data = await fetchJson<AzuroConditionResponse>(url, {
    method: "POST",
    headers: {
      "content-type": "application/json",
    },
    body: JSON.stringify({
      environment,
      gameIds,
    }),
  });
  return data.conditions ?? [];
}

export async function collectAzuro(maxMarkets: number): Promise<MarketSnapshotV1[]> {
  const snapshots: MarketSnapshotV1[] = [];

  for (const environment of DEFAULT_ENVIRONMENTS) {
    const games: AzuroGame[] = [];
    let page = 1;

    while (games.length < maxMarkets) {
      const batch = await fetchGames(environment, page);
      if (batch.length === 0) {
        break;
      }
      games.push(...batch);
      if (batch.length < 20) {
        break;
      }
      page += 1;
    }

    const gameMap = new Map<string, AzuroGame>();
    for (const game of games.slice(0, maxMarkets)) {
      gameMap.set(game.gameId, game);
    }

    for (const gameIdChunk of chunk([...gameMap.keys()], 10)) {
      const conditions = await fetchConditions(environment, gameIdChunk);
      for (const condition of conditions) {
        if (snapshots.length >= maxMarkets) {
          break;
        }
        const gameId = String((condition.game as { gameId?: string } | undefined)?.gameId ?? "");
        const game = gameMap.get(gameId);
        const source = {
          collector: "ts.azuro.backend_api",
          variant: "backend_api",
          status: "ok" as const,
          completeness: 1,
          endpoints: [
            `${AZURO_API_BASE}/api/v1/public/market-manager/games-by-filters`,
            `${AZURO_API_BASE}/api/v1/public/market-manager/conditions-by-game-ids`,
          ],
          notes: [`environment=${environment}`],
        };
        snapshots.push(
          toSnapshot(
            "azuro",
            String(condition.conditionId ?? condition.id),
            source,
            {
              environment,
              game,
              condition,
            },
          ),
        );
      }
      if (snapshots.length >= maxMarkets) {
        break;
      }
    }

    if (snapshots.length >= maxMarkets) {
      break;
    }
  }

  return snapshots.slice(0, maxMarkets);
}
