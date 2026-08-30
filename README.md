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
orchestrator/
  app.py            app factory, lifespan, the selector-loop entrypoint
  runtime.py        request-scoped dependencies; pool and services on app.state
  routes/           accounts.py  sync.py  data.py  pool.py
  background.py     scheduler loop, task tracking
  auth.py           bearer token

pool/
  manager.py        the priority queue and the dispatcher
  worker_handle.py  one worker process: spawn, pipe, restart ladder, counters
  worker.py         runs inside the child; one terminal, one task at a time
  protocol.py       what crosses the pipe

mt5api/
  terminal.py       the only module that imports MetaTrader5
  executors.py      account, deals, orders, positions - and waiting for history
  candles.py        candle windows, with the clock skew and the stray bars
  payload.py        one terminal read -> SyncPayload
  enrichment.py     MAE/MFE across a set of positions
  builders/         deals -> FX models (ported from Go); enrich.py has MAE/MFE and RR
  helpers/          ledger.py (balance reconstruction), candlespan.py, timeutil, mathutil
  raw.py            MT5 rows -> plain dataclasses

domain/             fx.py (canonical models), models.py (tables), enums.py
repositories/       queries and flush, no commits
services/           transaction boundaries: account, sync, query
utils/              config, logging, hashing, ids
```

## Endpoints

| Method | Path | |
|---|---|---|
| POST/GET/PATCH/DELETE | `/accounts`, `/accounts/{id}` | CRUD |
| POST | `/accounts?wait=true` | register, then block on the initial sync |
| POST | `/accounts/{id}/sync?wait=true` | hard sync, result in the response |
| POST | `/accounts/{id}/sync?wait=false` | 202, queued at normal priority |
| GET | `/accounts/{id}/positions?days=N` | closed positions |
| GET | `/accounts/{id}/open-positions` | live exposure as of the last sync |
| GET | `/accounts/{id}/balance-snapshots?days=N` | derived on read |
| GET | `/accounts/{id}/transactions?days=N` | deposits/withdrawals |
| GET | `/accounts/{id}/info` | balance, leverage, currency |
| GET | `/pool/status` | per-worker state, queue depth |
| GET | `/healthz` | public liveness |

`days=0` means "everything". Every route except `/healthz` takes a single
bearer token (`MT5_API_API_TOKEN`) in `Authorization: Bearer ...`; leaving the
setting empty disables auth entirely, which is only acceptable on a closed
network.

## Account status

Two states, and they answer one question only — can the pool reach this account?

| status | meaning |
|---|---|
| `active` | the last sync connected |
| `error_connection` | the pool has given up on the credentials or the server |

Why an account is unreachable lives in `last_error`; whether anyone *wants* it
synced is the separate `enabled` flag. `consecutive_failures` counts failed
syncs and resets to zero on the first success.

An account flips to `error_connection` when either:

* **three consecutive syncs fail** (`MT5_API_ACCOUNT_ERROR_THRESHOLD`), or
* **its very first sync fails.** Registering an account queues an *initial*
  sync ahead of everything else in the pool, with a longer connect timeout
  (`MT5_API_TERMINAL_INITIAL_CONNECT_TIMEOUT_MS`, 60s) than a routine one.
  There is no run of successes for that failure to be a blip in, so one strike
  is conclusive. `POST /accounts?wait=true` blocks on it and returns the
  settled status.

The scheduler **skips** `error_connection` accounts. It has to: an account that
has never synced has `last_synced_at IS NULL`, so it is due on every single
tick, and one wrong server name would otherwise re-queue itself every 60s and
occupy every worker for ~100s a time. They come back on an explicit
`POST /accounts/{id}/sync`, or as soon as `PATCH /accounts/{id}` supplies a new
password — new credentials clear the strikes and restore `active`.

## What the terminal actually requires

Everything here was found the hard way on Windows; none of it reproduces on a
Mac, where the package will not even import.

**`initialize()` cannot start a terminal without credentials.** A terminal with
no stored account sits on its *Open an Account* wizard and never answers the IPC
channel, so a bare `mt5.initialize(path=...)` just times out — on a fresh clone
*and* on one that logged in successfully an hour ago. Workers therefore start
the terminal lazily, on their first task, passing that task's login. Later tasks
switch accounts with `login()`.

**Read the error code, it is half a diagnosis.** The two failures look nothing
alike:

| result | code | took | means |
|---|---|---|---|
| `IPC timeout` / `Pipe server didn't answer` | `-1000x` | the full timeout | the terminal never got as far as logging in |
| `Authorization failed` | `-6` | 1–3s | the server answered and refused the credentials |

A `-6` is conclusive: the terminal resolved the server. A timeout is not — it is
*usually* a server the terminal has not been configured with, but a cold start
that cannot reach an otherwise-known server looks identical, and the same
account can give a timeout on one instance and a `-6` on another. Treat a
timeout as "check the server list first, then the credentials".
`mt5api/terminal.py` appends that reading to the message, so `last_error`
carries it. A dead IPC channel stays dead for every later call on
the module, so that case takes the terminal down with it and the next task
starts a fresh one; a rejected login leaves it usable.

**Timeouts.** 30s for a routine connect, on a terminal that is already up and
only switching accounts. 60s for the first sync of a newly added account, which
also has to start the terminal. Cold starts are the expensive case and they get
more expensive in parallel — several terminals coming up at once on one box
contend for the same CPU and the same server lookups — so if a whole pool
restarting produces timeouts that a single instance does not,
`MT5_API_TERMINAL_INITIAL_CONNECT_TIMEOUT_MS` is the knob, not a broken server
list.

**Dates near the unix epoch.** The package converts datetimes through the
platform's local-time functions, and Windows probes a day either side to resolve
DST. Anything within about a day of the epoch pushes that probe below zero,
where Windows answers `EINVAL` — surfacing as the memorable
`SystemError: <built-in function history_deals_get> returned a result with an
exception set`. `helpers/timeutil.py` therefore floors a full history pull at
1971-01-01.

**Logins do not fit in an INTEGER.** MT5 account numbers run to ten digits, past
2^31, so `MT5Account.login` is a `BigInteger`.

**History arrives after `login()` does.** On a funded account
`history_deals_total` reads 0 for the first seconds and then jumps - measured at
0 on return and 188 two seconds later. A sync reading through that window
reconstructs an empty account, and the ledger then derives `BalanceInit` from a
balance with no deals to explain it, so the damage is quietly wrong numbers
rather than a failure. `executors.wait_for_history` blocks until the count
settles, and refuses to believe a zero on an account that holds money.

**`copy_rates_range` reads its arguments in a different clock than it answers
in.** The datetimes handed to it are taken as trade-server time, while the bar
times it returns line up with deal times: against a UTC+3 server, asking for
12:00-14:00 returns bars stamped 09:00-11:00. Trimming that to the window asked
for leaves only the overlap, which is *empty* for any position shorter than the
offset - it left 61 of one account's 91 positions with no candles and quietly
truncated the rest. `fetch_candles` widens the request past any real server
offset and trims by bar time, rather than calibrating a per-broker offset that
would need re-deriving twice a year for daylight saving.

**`copy_rates_range` does not report "nothing here" as empty.** Asked for three
hours in 2022 on a symbol whose minute history reaches back 70 days, it returns
a single unrelated bar from years later. Fed to the MAE/MFE builder that turned
a 0.01 lot EURUSD position into a 161 EUR excursion on a 109 EUR account. Every
row is now checked against the window it was meant to come from, and no candles
means no MAE/MFE rather than a confident wrong answer.

**The fills are part of the range, because the candles are only half the
market.** Bars are bid; a short is closed by buying at the ask, so its exit
price sits a spread *above* the bid high it traded in. Four of one account's
positions had exits 0.2 to 2.1 pips outside their own candles for exactly that
reason. `apply_fx_mae_mfe` folds the entry and exit into the high and low, so
`MAE <= Pnl <= MFE` holds by construction rather than by luck - and those four
get measured instead of discarded.

What is checked instead is *overlap*: candles that do not straddle the prices a
position actually dealt at belong to some other period, and are refused. That
is the case worth catching - one stray bar from years later turned a 0.01 lot
EURUSD position into a 161 EUR excursion on a 109 EUR account.

**Some positions can never be measured, and say so.** An account here traded
`XRPUSD_i` in 2022; the broker has since dropped the symbol, so `symbol_info`
answers `Not found` and no history exists at any depth. Those eight positions
carry `MAE = None` rather than a number derived from nothing.

**How far back MAE/MFE can see is a terminal setting.** `Max bars in chart`
caps the depth of every timeseries, so at the default 100 000 the minute series
only reaches ~70 days and no amount of waiting produces 2022 data. The master
sets it to unlimited. Daily bars are unaffected either way - 100 000 of them is
270 years. `MT5_API_ENRICH_MAE_MFE=false` opts out of the whole business; RR
needs no candles and keeps working.

**A minute window in the past is never cheap.** The terminal cannot serve an
isolated old window: it downloads whole years and materialises a contiguous
series from the requested date to today. One three-hour request in 2022 landed
83 MB of yearly `.hcc` files plus an 83 MB built `M1.hc` - 170 MB for a single
symbol on a single instance, and every instance keeps its own copy.

That is why enrichment is **incremental**. A sync still rebuilds the entire
history, because the ledger needs every deal to recover `BalanceInit`, but a
closed position's excursion never changes once measured. `SyncService.request`
reads back the ids that already carry a figure and hands them to the worker,
which prices only the rest. Without it every sync would ask for candles from
years ago and the deep cache could never be reclaimed. The flip side: a skipped
position arrives carrying no MAE/MFE, so `PositionRepository` treats an absent
value as "not recalculated" rather than "cleared" - otherwise the second sync's
upsert would wipe what the first one measured.

With that in place the cache is disposable, and `deploy/prune-history.ps1`
disposes of it. Deleting is safe while the pool runs: Windows refuses the few
files a terminal holds open and the script skips them, the terminal keeps
serving from memory meanwhile, and anything still needed comes back on demand
in about 30s. Measured across three instances after a backfill: 2.8 GB down to
0.6 GB.

**Stripping the terminal down does not stay stripped.** `MetaEditor64.exe` and
`metatester64.exe` are not needed to sync, but LiveUpdate pulls them back the
first time an instance is used, so the master ships them rather than have every
clone re-download 133 MB. Removing the `Sounds` wavs and the MQL5 sample
sources does stick, and the samples are worth removing - the terminal
recompiles them on a fresh start.

**uvicorn hands itself the wrong event loop.** psycopg refuses to run its async
mode on a `ProactorEventLoop`, and uvicorn selects one on Windows through an
explicit `loop_factory` that ignores the event loop policy. `run()` drives the
server on a `SelectorEventLoop` instead.

## Deploying

Needs **Windows** — `MetaTrader5` is Windows-only, and so is everything the
terminal does. The repository only carries the service; the four things below
live outside it and have to exist on the box before the service is any use.

### 1. Machine

* **Python 3.12, 64-bit.** The `MetaTrader5` wheel is CPython-specific and
  64-bit only, to match `terminal64.exe`.
* **PostgreSQL.** Any recent version. Create the role and database the
  connection string expects:

  ```sql
  CREATE ROLE mt5_api LOGIN PASSWORD '...';
  CREATE DATABASE mt5_api OWNER mt5_api;
  ```

  Tables are created at startup (`SQLModel.metadata.create_all`); there are no
  migrations, so a change to a column type or an enum needs the database
  dropped rather than altered.

### 2. The master terminal

Install MetaTrader 5 once, anywhere, then keep one cleaned copy as the master —
say `D:\MT5\master`. Every instance is a *portable* install: its data lives
beside `terminal64.exe` rather than in `%APPDATA%\MetaQuotes`, which is what
makes a clone a directory copy and keeps instances from sharing state.

Open it with `deploy\open-master.cmd`, which passes `/portable`. Opening it any
other way sends it to `%APPDATA%\MetaQuotes`, and the brokers added there will
never reach the clones.

Add **every broker server you intend to use**, through *File → Open an Account*.
`mt5.login` cannot reach a server the terminal has never heard of, and
`Config\servers.dat` is only written when the terminal exits — so close it fully
before cloning.

The master also carries the settings every clone inherits, which are worth
checking after any manual session with it:

| setting | why |
|---|---|
| *Max bars in chart*: unlimited | without it MAE/MFE cannot see past ~70 days |
| News, sounds, notifications off | nothing in a sync needs them |
| No chart profiles | an open chart renders ticks for no reason |
| `Config\assistant.ini`: both MCP listeners `Enable=0` | they bind fixed ports 22345 and 22346, so the second instance would collide |

### 3. The pool

```powershell
deploy\clone.ps1 -Count 6 -Root D:\MT5
```

Run it with every terminal closed. It copies the master N times, strips the
per-instance caches, and prints the `MT5_API_TERMINAL_PATHS` line to paste into
`.env`. `-Force` rebuilds clones that already exist.

### 4. The service

```powershell
pip install -r requirements.txt
copy .env.example .env      # then edit it
python -m orchestrator.app
```

The settings that decide whether it works at all:

| setting | note |
|---|---|
| `MT5_API_TERMINAL_PATHS` | the number of paths *is* the pool size |
| `MT5_API_TERMINAL_PORTABLE` | must match how the instances were installed; a mismatch sends the terminals to a data directory with no accounts in it |
| `MT5_API_DATABASE_URL` | |
| `MT5_API_API_TOKEN` | leaving it empty disables auth on every route but `/healthz` |

Startup logs an `app.startup.config` warning for each terminal path that is not
there, an empty pool, and a missing token. Then check `GET /healthz` and
`GET /pool/status`: one worker per path, all healthy.

### 5. Housekeeping

Schedule `deploy\prune-history.ps1 -Apply` hourly. Without it the price cache
grows without limit — see *A minute window in the past is never cheap* above.
Run without `-Apply` to see what it would reclaim.

```powershell
schtasks /Create /SC HOURLY /TN "MT5 prune history cache" /F ^
  /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\path\to\deploy\prune-history.ps1 -Apply"
```

It is safe against a live pool: files a terminal holds open are skipped, and
anything still needed is re-downloaded on demand.

### What it costs

Measured with six terminals on one box:

| | |
|---|---|
| idle terminal | ~130 MB RSS |
| terminal mid-backfill | ~500 MB RSS |
| six terminals + API + six worker processes | ~1.6 GB peak observed |
| worst case, all six backfilling at once | ~3.6 GB |
| disk, per instance | 250 MB, plus the price cache the prune keeps in check |

A cold terminal start is about twelve seconds and they are serialised across the
pool, so a six-worker pool takes roughly a minute to have every terminal up —
but only as tasks arrive, since a terminal cannot start before a task supplies
credentials.

## Verifying

`/healthz` and the account statuses cover the pool. What no test here can cover
is the reconstruction itself, which needs an account with real trading history:

1. Hard-sync it, then reconcile a few positions against the terminal's History
   tab: `NetPnl`, `Commission`, `Swap`, and especially `BalanceInit` — a
   mismatch there points straight at the ledger.
2. For MAE/MFE, open the chart over a position's lifetime and check the high and
   low match. A symbol the account has not traded recently may need selecting in
   Market Watch before its history is available.
3. Queue a bulk sync of 20+ accounts and watch `/pool/status`: every worker
   busy, the queue draining, and a hard sync fired mid-way coming back first.
4. Kill one `terminal64.exe` mid-sync — the service should restart that worker
   and finish the task.
