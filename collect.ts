import { loadDotEnv } from "./ts/env.js";
import type { Exchange, MarketSnapshotV1 } from "./ts/types.js";

loadDotEnv();

async function run(): Promise<void> {
  const [
    { defaultOutPath, parseArgs, writeSnapshotsGzip },
    { collectAzuro },
    { collectKalshi },
    { collectMyriad },
    { collectOpn },
    { collectPolymarket },
    { collectSxBet },
  ] = await Promise.all([
    import("./ts/utils.js"),
    import("./ts/collectors/azuro.js"),
    import("./ts/collectors/kalshi.js"),
    import("./ts/collectors/myriad.js"),
    import("./ts/collectors/opn.js"),
    import("./ts/collectors/polymarket.js"),
    import("./ts/collectors/sxbet.js"),
  ]);

  const collectors: Record<Exchange, (maxMarkets: number) => Promise<MarketSnapshotV1[]>> = {
    polymarket: collectPolymarket,
    opn: collectOpn,
    azuro: collectAzuro,
    sxbet: collectSxBet,
    myriad: collectMyriad,
    kalshi: collectKalshi,
  };

  const args = parseArgs(process.argv.slice(2));

  const outPath = args.out ?? defaultOutPath(args.exchange);
  const collector = collectors[args.exchange];

  const snapshots = await collector(args.maxMarkets);
  await writeSnapshotsGzip(outPath, snapshots);

  process.stdout.write(
    JSON.stringify(
      {
        exchange: args.exchange,
        out: outPath,
        snapshots: snapshots.length,
      },
      null,
      2,
    ) + "\n",
  );
}

run().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.stack : String(error)}\n`);
  process.exitCode = 1;
});
