# MT5 Sync API

Keeps a pool of MetaTrader 5 terminals busy syncing trading accounts into
**standardized FX models** — the same shapes the Go reconstruction library
produces for cTrader, so a consumer cannot tell the two sources apart.

## Why it looks like this

MT5 hands you **deals**, not positions with P&L. Reconstructing positions is the
same problem the Go cTrader connector already solved, so the builders here are a
port of `pkg/ctrader/service/reconstructor/builders/` — group deals by position
id, require a closing deal, `net = pnl + swap − commission − fee`.

The `MetaTrader5` package is a **process-global singleton**: `initialize(path=…)`
binds the whole module to one terminal and `login()` switches the account inside
it. Two terminals in one process is impossible and threads do not help. So:

```
FastAPI parent (asyncio)          -> Postgres (all normal reads)
 ├─ PoolManager
 │   ├─ asyncio.PriorityQueue     hard sync = 0, scheduled = 1
 │   ├─ idle worker queue         a freed terminal takes the next task
 │   └─ dispatcher
 ├─ N worker processes            one per terminal, pipe to the parent
 └─ scheduler                     re-queues accounts older than the interval

worker process
  mt5.initialize(path=…) once at start
  loop: recv task -> mt5.login(...) -> executors -> builders -> send models
```

The queue lives in the parent because a `multiprocessing.Queue` has no priority
and a hard sync has to jump the line. Children only ever report "I am free".

**Hard sync** (`POST /accounts/{id}/sync?wait=true`) is queued at priority 0 and
its result is returned in the response — already written to the database. Every
other endpoint reads from Postgres.

## Data model

Canonical models live in `domain/fx.py` and mirror the Go structs field for
field. Go marshals them with PascalCase names (the structs carry no json tags),
so these pydantic models keep snake_case attributes but serialize by alias to
`ID`, `NetPnl`, `BalanceInit`, …

| Model | Source |
|---|---|
| `FXPosition` (+ `FXOrder`, `FXTrade`) | deals grouped by `position_id` |
| `FXOpenPosition` | `positions_get()` |
| `FXAccountInfo` | `account_info()` — balance, leverage, currency |
| `Transaction` | non-trading deals (`DEAL_TYPE_BALANCE`, credit, …) |
| `UserBalanceSnapshot` | derived from positions, never stored |
| `MAE` / `MFE` | candles the position lived through |
| `RR` / `RRPlanned` | entry, stop loss, take profit — no candles involved |

### BalanceInit, the one real difference from cTrader

cTrader puts the account balance on every closing deal, so the Go builder does
`BalanceInit = balanceAfterClose − net`. **MT5 deals carry no balance.**
`mt5api/helpers/ledger.py` rebuilds it: sum every deal's balance effect, subtract
that from the *current* balance to recover the starting balance, then replay
forward and record the balance after each position's last close.

Consequence: **a sync always pulls the full history**; the `days` filter is
applied after the models are built. A windowed pull would recover the wrong
starting balance. (The Go code does the same — `GetBalanceSnapshots` calls
`GetBuiltPositions(days=0)` and filters afterwards.)

### MAE/MFE and RR

Both hang off one derived quantity: **money per unit of price movement**, which
the Go builder recovers from the position itself as `|Pnl / priceDelta|`. That
sidesteps contract sizes and deposit-currency conversion completely — no
`symbol_info` lookup, no FX cross rates.

`MAE`/`MFE` are the worst and best unrealised excursions in deposit currency
(MAE <= 0, MFE >= 0), taken from the high and low of the candles between
`CreatedAt` and `ClosedAt`. Candle pulls dominate sync time, so the span is
split into minute bars at the edges and daily bars in between
(`helpers/candlespan.py`) — exact for a high/low, and a position open for
months does not drag in months of minute bars. Set
`MT5_API_ENRICH_MAE_MFE=false` to skip it.

`RR` needs no candles — it is the realised result over the risk the trade was
opened with:

```
RR        = NetPnl / (|entry − SL| × money-per-price-unit)
RRPlanned = |TP − entry| / |entry − SL|
```

A trade stopped out for its full risk lands on `RR = -1`. `RRPlanned` is a ratio
of two price distances, so the money factor cancels. Both stay `None` without a
stop loss. `NetPnl` is used rather than `Pnl`, so RR reflects what the trader
actually kept after commission and swap.

Because RR must measure the *original* risk, `_extract_protection` keeps the
**first** stop loss it sees, not the last — otherwise a stop trailed to
breakeven would erase the risk it was taken with. This is a deliberate
divergence from the Go version, which keeps the last.

## Layout

```
orchestrator/app.py     FastAPI routes + lifespan
orchestrator/background.py   scheduler loop, task tracking
pool/{manager,worker,protocol}.py   the terminal pool
mt5api/terminal.py      the only module that imports MetaTrader5
mt5api/executors.py     history_deals_get / history_orders_get / positions_get
mt5api/builders/        deals -> FX models (ported from Go)
mt5api/builders/enrich.py    MAE/MFE and RR
mt5api/enrichment.py    candle fetching for MAE/MFE
mt5api/helpers/ledger.py     balance reconstruction
mt5api/helpers/candlespan.py minute bars at the edges, daily in between
domain/                 fx.py (canonical), models.py (tables), payload.py
repositories/           flush only
services/               commit here
```

## Endpoints

| Method | Path | |
|---|---|---|
| POST/GET/PATCH/DELETE | `/accounts`, `/accounts/{id}` | CRUD |
| POST | `/accounts/{id}/sync?wait=true` | hard sync, result in the response |
| POST | `/accounts/{id}/sync?wait=false` | queue at normal priority |
| GET | `/accounts/{id}/positions?days=N` | closed positions |
| GET | `/accounts/{id}/open-positions` | live exposure as of the last sync |
| GET | `/accounts/{id}/balance-snapshots?days=N` | derived on read |
| GET | `/accounts/{id}/transactions?days=N` | deposits/withdrawals |
| GET | `/accounts/{id}/info` | balance, leverage, currency |
| GET | `/pool/status` | per-worker state, queue depth |
| GET | `/healthz` | public liveness |

`days=0` means "everything". Auth is a single bearer token
(`MT5_API_API_TOKEN`); leaving it empty disables auth.

## Running

Needs **Windows** with the terminals installed — `MetaTrader5` is Windows-only.

1. Install 6–10 separate copies of the terminal, each in its own folder, and add
   every broker server you intend to use to each one. `mt5.login` cannot reach a
   server the terminal does not know.
2. `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and set `MT5_API_TERMINAL_PATHS` and
   `MT5_API_DATABASE_URL`. The number of paths *is* the pool size.
4. `python -m orchestrator.app`
5. Check `GET /healthz` and `GET /pool/status` — one worker per path.

Tables are created at startup (`SQLModel.metadata.create_all`); there are no
migrations.

## Verifying on Windows

Nothing about the terminal can be checked off Windows, so verify by hand after
the first run:

1. `GET /healthz` and `GET /pool/status` — one worker per configured path.
2. Hard-sync one demo account, then reconcile a few positions against the
   terminal's History tab: `NetPnl`, `Commission`, `Swap`, and especially
   `BalanceInit` — a mismatch there points straight at the ledger.
3. For MAE/MFE, open the chart over a position's lifetime and check the high
   and low match. A symbol the account has not traded recently may need
   selecting in Market Watch before its history is available.
4. Queue a bulk sync of 20+ accounts and watch `/pool/status`: every worker
   busy, the queue draining, and a hard sync fired mid-way coming back first.
5. Kill one `terminal64.exe` mid-sync — the service should restart that worker
   and finish the task.
