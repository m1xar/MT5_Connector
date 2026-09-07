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
 │   └─ per terminal: PriorityQueue (hard sync = 0, scheduled = 1) + dispatcher
 ├─ N worker processes            one per terminal, pipe to the parent
 └─ scheduler                     re-queues accounts older than the interval

account.terminal_path             which terminal's queue every sync of it goes to

worker process
  loop: recv task -> terminal.connect(...) -> fetch -> builders -> send models
```

The terminal is *not* started when the worker is. It cannot be: `initialize()`
without credentials leaves the terminal sitting on its account wizard, where it
never answers the IPC channel. It comes up on the first task that supplies a
login, and later tasks switch accounts with `login()`.

The queues live in the parent because a `multiprocessing.Queue` has no priority
and a hard sync has to jump the line. There is one per terminal, not one for the
pool: **every account is pinned to a terminal** when it is registered — the one
with the fewest active accounts at that moment, reported as `terminal` in the
account response — and every sync of it, now and later, runs on that terminal
only. Brokers count logins per device; an account that appears from twelve
terminals in a day looks like a credential-stuffing run and gets its history
withheld or its login refused, measured. The pin is the path string from
`MT5_API_TERMINAL_PATHS`; an account whose path has left the config is re-pinned
to the least-loaded remaining terminal on its next sync. A hard sync jumps the
line of its own terminal.

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
| `*Utc` timestamps | the unsuffixed field minus the server's offset *as it stood then* |

Four tables are stored: accounts, closed positions, open positions and
transactions. Sync runs are deliberately not one of them — a sync is either in
flight, and then `/pool/status` knows about it, or finished, and then its
outcome is on the account (`last_synced_at`, `status`, `consecutive_failures`,
`last_error`). Every log line of one sync still shares a correlation id, but it
identifies nothing queryable and is not in any response.

### BalanceInit, the one real difference from cTrader

cTrader puts the account balance on every closing deal, so the Go builder does
`BalanceInit = balanceAfterClose − net`. **MT5 deals carry no balance.**
`builders/position.py` rebuilds it: sum every deal's balance effect, subtract
that from the *current* balance to recover the starting balance, then replay
forward and record the balance after each position's last close.

Consequence: **a sync always pulls the full history**; the `days` filter is
applied after the models are built. A windowed pull would recover the wrong
starting balance. (The Go code does the same — `GetBalanceSnapshots` calls
`GetBuiltPositions(days=0)` and filters afterwards.)

### MAE/MFE and RR

Both hang off one derived quantity: **money per unit of price movement**, which
the Go builder recovers from the position itself as `|Pnl / priceDelta|`. `Pnl`
is the gross result — commission and swap come off only in `NetPnl` — so that
ratio is exactly what one unit of price was worth to this position. It
sidesteps contract sizes and deposit-currency conversion completely: no
`symbol_info` lookup, no FX cross rates.

That is not a stylistic preference. Measured across three brokers, the
`symbol_info` route is wrong wherever the profit currency is not the deposit
currency — `trade_contract_size` alone was out by the FX cross (GER30 by 1.159,
USDCAD by 0.720, USDCHF by 1.238), and by the cross *as it stood then* for an
old position rather than today's. `trade_tick_value`, which exists to solve
that, came back `0.0` for both USDCAD and USDCHF and in EUR rather than USD for
GER30. A delisted symbol has no `symbol_info` at all.

**A position that closed at its entry price cannot answer**: `Pnl` and the price
delta are both zero, and 0/0 is all it has. The factor is linear in size, so
`value_per_lot` takes it from another position on the same symbol and scales by
lots. Failing that the position carries no `MAE`, `MFE` or `RR` — `Amount` is
lots, not money, and substituting one for the other is wrong by the contract
size, which on a 0.23 lot EURUSD position is a factor of 100 000.

`MAE`/`MFE` are the worst and best unrealised excursions in deposit currency
(MAE <= 0, MFE >= 0), taken from the high and low of the candles between
`CreatedAt` and `ClosedAt`. Candle pulls dominate sync time, so the span is
split into minute bars at the edges and daily bars in between
(`enrichment._segments`) — exact for a high/low, and a position open for
months does not drag in months of minute bars. Set
`MT5_API_ENRICH_MAE_MFE=false` to skip it.

`RR` needs no candles — it is the realised result over the risk the trade was
opened with:

```
RR        = NetPnl / (|entry − SL| × money-per-price-unit)
RRPlanned = |TP − entry| / |entry − SL|
```

A trade stopped out for its full risk lands on `RR = -1`. `RRPlanned` is a ratio
of two price distances, so the money factor cancels — it is still answered when
the factor cannot be recovered at all. Both stay `None` without a stop loss. `NetPnl` is used rather than `Pnl`, so RR reflects what the trader
actually kept after commission and swap.

Because RR must measure the *original* risk, `_extract_protection` keeps the
**first** stop loss it sees, not the last — otherwise a stop trailed to
breakeven would erase the risk it was taken with. This is a deliberate
divergence from the Go version, which keeps the last.

## Layout

```
orchestrator/
  app.py            app factory, lifespan, the selector-loop entrypoint
  deps.py           bearer auth and request-scoped dependencies
  routes/           accounts.py  sync.py  data.py  pool.py

pool/
  manager.py        one priority queue and one dispatcher per terminal; terminal affinity
  worker_handle.py  one worker process: spawn, pipe, restart ladder, counters
  worker.py         runs inside the child; one terminal, one task at a time
  protocol.py       what crosses the pipe

mt5api/
  terminal.py       the only module that imports MetaTrader5; start gate, probe
  fetch.py          every read off a terminal: account, deals, orders, candles, clock cache
  clock.py          the server's UTC offset per switch, read off the trading week
  enrichment.py     money factors, RR, MAE/MFE
  payload.py        one terminal read -> SyncPayload
  builders/         deals -> FX models (ported from Go); position.py carries the ledger
  raw.py            MT5 rows -> plain dataclasses
  timeutil.py       the epoch floor and the server-clock/UTC tagging rules
  numbers.py        rounding
  cache.py          price-cache prune

domain/             fx.py (canonical models and SyncPayload), models.py (tables), enums.py
repositories/       queries and flush, no commits
services/           transaction boundaries: account, sync (with the scheduler), query; terminal_affinity
utils/              config, logging

deploy/             open-master, clone, prune-history
```

## Endpoints

| Method | Path | |
|---|---|---|
| POST/GET/PATCH/DELETE | `/accounts`, `/accounts/{id}` | CRUD |
| POST | `/accounts` | register, then block on the initial sync; 502 if it did not complete |
| POST | `/accounts/{id}/sync?wait=true` | hard sync, result in the response |
| POST | `/accounts/{id}/sync?wait=false` | 202, queued at normal priority |
| GET | `/accounts/{id}/positions?days=N` | closed positions |
| GET | `/accounts/{id}/open-positions` | live exposure as of the last sync |
| GET | `/accounts/{id}/balance-snapshots?days=N` | derived on read |
| GET | `/accounts/{id}/transactions?days=N` | deposits/withdrawals |
| GET | `/pool/status` | per-worker state, its own queue depth, accounts pinned to it |
| GET | `/healthz` | public liveness: `ok`, `degraded`, `stalled` |

`days=0` means "everything". `/healthz` reports `degraded` when a terminal is
unusable and `stalled` when the dispatcher has stopped — the second one matters
because every worker can look perfectly healthy while nothing is being handed
to them. Every route except `/healthz` takes a single
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

Registration always waits for that sync, and answers **502** with the
account id and the error if it did not complete — an importer that saw 201 would
tick the account off as done while every read of it returned nothing.

An account flips to `error_connection` when either:

* **three consecutive syncs fail** (`MT5_API_ACCOUNT_ERROR_THRESHOLD`), or
* **its very first sync fails.** Registering an account queues an *initial*
  sync ahead of everything else in the pool, with a longer connect timeout
  (`MT5_API_TERMINAL_INITIAL_CONNECT_TIMEOUT_MS`, 120s) than a routine one.
  There is no run of successes for that failure to be a blip in, so one strike
  is conclusive. `POST /accounts` blocks on it and returns the settled status.

The scheduler **skips** `error_connection` accounts. It has to: an account that
has never synced has `last_synced_at IS NULL`, so it is due on every single
tick, and one wrong server name would otherwise re-queue itself every 60s and
occupy every worker for ~100s a time. They come back on an explicit
`POST /accounts/{id}/sync`, or as soon as `PATCH /accounts/{id}` supplies a new
password — new credentials clear the strikes and restore `active`.

## When the pool goes wrong

A sync pool has a failure mode worse than crashing: staying up and quietly
doing nothing. Every guard below exists to make that impossible or, failing
that, visible.

**A dispatcher outlives its own bugs.** Each terminal has a loop that feeds it
from its queue; if one died, that terminal's tasks would pile up for ever while
the worker still reported itself healthy. So its body is guarded and it keeps
going, and `/healthz` reports any dead loop as `stalled` — a state no worker
count can express.

**A task always ends, and never moves.** Anything thrown while running one is
caught, the future is resolved, and the worker either becomes ready again or is
marked failed. A failed worker's queue keeps its tasks and waits: the reaper
tries a restart once a minute, and the tasks run on that terminal when it is
back. They are never handed to another terminal — that is the whole point of
the pin — so a hard sync on a dead terminal times out with 504 while its task
stays queued. Before, only four exception types were handled; anything else
removed a terminal from the pool without a word and left the caller waiting out
its own timeout.

**Failing to spawn is a normal outcome**, not an exception - the restart ladder
has to keep its footing whether the process would not start or would not
answer.

**Nothing writes after shutdown.** In-flight tasks are tracked and awaited by
`stop()`, so a result can no longer be persisted against a disposed engine.

**Threads are sized to the pool.** Every in-flight sync parks one thread in a
blocking pipe read for its whole duration, up to the task timeout. The default
executor is `min(32, cpu_count + 4)` and shared with everything else in the
process, so the pool sets its own rather than discovering the ceiling in
production.

## Time, and whose clock it is

Every timestamp MT5 hands out - deal times, order times, bar times - is stamped
in the **trade server's** clock while looking exactly like a UTC epoch. Nothing
in the API tells you which clock that is.

That is internally consistent, so every calculation is right: deals, positions
and candles all live in the same scale. It is only wrong at the edges, where a
consumer reasonably assumes UTC.

So the offset is measured and reported rather than guessed at — not one
offset, but the whole history of it, since a broker that keeps daylight saving
was not on today's offset last January. The result is stored on the account as
`server_clock`, a step per switch, and the offset in force *now* is shown as
`server_utc_offset_minutes` in `GET /accounts/{id}`.
Timestamps come back in pairs — `ClosedAt` is what the terminal itself would
show, `ClosedAtUtc` is the same instant in real UTC:

```json
{ "ClosedAt": "2025-03-14T15:30:00", "ClosedAtUtc": "2025-03-14T13:30:00Z" }
```

Only the `*Utc` twin carries a `Z`. The unsuffixed field deliberately does not:
it is the server's own clock, and labelling it UTC would be a lie that survives
all the way to a consumer, who would parse it confidently and be wrong by the
offset. `raw.py` therefore strips the marker MT5 implies rather than passing it
on.

### Measuring it off the week, not the clock

There is nothing to ask. `terminal_info` and `account_info` carry no time field
at all, and asking for bars by date proves nothing: the package reduces the
argument to an epoch and compares it straight against the stamps, which are
themselves server labels dressed as UTC. Ask for `12:00` and a bar stamped
`12:00` comes back — request and response in the same scale, with no real UTC
anywhere in the loop. That is why `as_mt5_time` tags the argument UTC: to make
that conversion the identity, not to expose an offset.

Two independent readings answer instead, and they divide the work.

**The live tick owns *now*.** `symbol_info_tick` carries the server's clock and
this machine carries real UTC, so the difference is the offset — one call,
nothing downloaded, exact to the hour on all three brokers measured. The catch
is the weekend: forex quotes stop, and Friday's last tick keeps reading as a
plausible offset that drifts an hour further out with every hour that passes. So
it is only trusted inside the trading week, and that window is known in real UTC
from the zone database without asking the terminal — no circularity. Whether the
tick has *moved* recently is not a usable test: a thin feed changed its stamp
once in six seconds while a busy one changed five times, and the thin one was
right.

The exception is an instrument that trades *through* the weekend. Its tick is
still fresh when every forex tick has gone stale, so the guard does not apply to
it — and which instruments those are is measured rather than assumed: the scan
below already reads a year of bars, and one with no weekend gap at all is, by
definition, trading through them. Useless for the scan, and on a Saturday the
only thing that can answer.

**The weekend scan owns history**, which the tick cannot speak to at all. Forex
ends its week at 17:00 in New York, a known instant in real UTC for any date.
That boundary appears in the bar labels as the last bar before the weekend gap,
stamped in the server's clock, and the difference between the two is the offset:

```
closed_at = label(last bar before the gap) + 1h   an hourly bar is labelled by its open
reading   = closed_at − (17:00 New York, in real UTC)
```

Bars are pulled **by position**, so no date is ever sent to the terminal and
nothing can be shifted on the way in. One call for 6 000 hourly bars covers a
year — about 52 weekends, both switches, and plenty of neighbours for the rules
below. It used to ask for 60 000. Reading them was free either way, since the
analysis runs over an array already in memory, but *fetching* them was not: the
terminal materialises whole years for any window it is asked for, which came to
~200 MB per symbol per instance and, on a cold terminal with three others
starting beside it, did not finish inside the task timeout at all — the scan
walked through four candidate symbols, ~800 MB, and still ended in
`clock.unknown`. A year costs ~21 MB. The trade is depth: `*Utc` is null older
than the scan reached, so a year of history converts.

A cold symbol answers the first call with nothing and starts downloading in the
background, so "not enough bars" means "not yet" rather than "wrong symbol".
Each candidate is therefore re-read for up to 30s before it is given up on,
which costs one download instead of five.

Only the Friday edge is read. The Sunday one looks like it should say the same
thing and does not: for the three weeks each March when New York has moved to
summer time and Europe has not, the broker measured here still opened its week
at midnight server time — an hour after the market itself — and that edge reads
an hour high for every one of them. The close follows the market; the open
follows the broker's session table.

### Daylight saving, closed

Every weekend in the scan is read, not just the latest, so the offset is known
as it stood on each of them. Collapse the readings to the points where they
change and you have the broker's own daylight saving schedule, measured rather
than assumed — no calendar, and no guess about whether a broker follows the
European rule or the American one. A timestamp is then converted with the
offset that was in force when it was stamped:

```
2025-01-15  ClosedAt 12:00 → ClosedAtUtc 10:00Z   (UTC+2)
2025-07-15  ClosedAt 12:00 → ClosedAtUtc 09:00Z   (UTC+3)
```

Two rules separate a switch from a holiday, and both come from the same fact:
**a reading can only come in low.** The market really does stop at 17:00 in New
York, so the last bar of the week cannot sit *after* it, while a broker that
shuts early for a holiday leaves one sitting well before it — over the last year
Thanksgiving and Independence Day moved a gold reading by two and four hours. So
a change is only believed when it is *exactly an hour* and the next **two**
weekends still agree.

Two, not one. One is enough for a lone holiday and not for a run of them:
Christmas and New Year close early in consecutive weeks, and on one account they
did so by exactly an hour each, which a single confirmation accepted as a switch
and left every timestamp in that fortnight an hour out. A tail too short to
confirm counts as unconfirmable rather than confirmed, which matters most of all
there — the window always ends *now*, so a sync in early January has exactly
those two weekends sitting in that position.

Demanding two would once have been unaffordable, because the tail is where a
real switch shows up first and refusing to believe it for a fortnight is its own
bug. The tick closes that gap: when it disagrees with the scan, a step is
appended from the start of the current trading week, which is exactly the window
the scan cannot yet confirm. A switch only ever happens while the market is
shut, so no timestamp lands between that boundary and the change itself.

Measured over the last year on three brokers: 152 weekend readings, three of
them moved by a holiday, all three isolated and all three dropped. The steps
that survived are the correct US switch dates — and on one broker the correct
*European* ones until 2024, when it changed rule. No calendar would have caught
that; the measurement did.

Each step is keyed by the first bar after the weekend the switch happened in.
Both ends of that weekend are known — the change shows up at one Friday close
and not the one before it, and the only Sunday between them is inside that gap —
and no trade is stamped while the market is shut, so nothing lands in the part
that is ambiguous.

**`*Utc` is null when the clock cannot be measured**, and for a timestamp older
than the scan reached, which is a better answer than a wrong one. When no symbol
can carry the scan but a tick is available, the tick alone answers for the
current week rather than leaving everything null.

The stored value is never rewritten: converting is a read-side concern, exactly
like the `days` window — which is itself translated into the server's clock
before it is compared against anything, for the same reason. `to_utc` always
tags its result UTC whatever it was handed, so every `*Utc` field carries the
`Z` and none is quietly naive; a hard sync fills the same pair from the clock
that read them, so the response and a later read agree.

The clock is measured once per broker per worker process and reused for six
hours. It changes twice a year, and re-reading it on every sync would mean
re-downloading the year behind it after each prune. A measurement that *fails*
is remembered just as long, keyed on the symbols that failed rather than on the
broker, so an account whose only instrument has no continuous minute history
pays the candidate walk — five symbols, 30 s each — once per worker process every six hours, not on every sync;
another account on the same broker with other instruments still gets its own
attempt. A broker whose clock was ever read keeps that reading through a later
failed attempt rather than dropping to unknown.

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
carries it.

**An IPC timeout does not mean the terminal is dead, so ask it.** A dead
terminal really does take its worker with it: the MetaTrader5 package is a
process-global singleton, and once the channel to a killed terminal has gone
every later call in that process fails instantly — all six terminals killed
under load left the pool reporting six healthy idle workers while every sync
failed in a tenth of a second. But the same `-10005` comes back from a *live*
terminal asked to log in to a server it does not know, and measured on one:
`terminal_info()` answers immediately afterwards and the next `login()` lands
in 0.7s. So every IPC failure is followed by one `terminal_info()` probe. No
answer retires the worker process, restarts it, and retries the task on the same
terminal, spending one retry. An answer leaves the terminal alone and the
failure is the account's. Before the probe,
one misspelt server name walked through all four workers, reset two terminals
that had just synced perfectly well, and took eleven minutes to say
`error_connection`.

A failed cold start behaves the same way: `terminal64.exe` stays up with no
account, and the next `initialize()` on that path attaches to it in under a
second rather than starting another.

**An initial sync gets one attempt.** On the account's terminal, then the
account is `error_connection` and the caller gets 502 — even when that terminal
turns out to be dead. Every clone shares the master's server list, so a server
one cannot reach none can, and a transport failure on the first sync is far
more often the account than the terminal.

**Timeouts.** 30s for a routine connect, on a terminal that is already up and
only switching accounts. 120s for the first sync of a newly added account, which
also has to start the terminal. Cold starts are the expensive case and they get
more expensive in parallel — several terminals coming up at once on one box
contend for the same CPU and the same server lookups — so if a whole pool
restarting produces timeouts that a single instance does not,
`MT5_API_TERMINAL_INITIAL_CONNECT_TIMEOUT_MS` is the knob, not a broken server
list.

Cold starts are staggered through a lock shared by the workers, and nobody
waits on it for more than 20s after the current start began. That is enough
for the process to come up — a start on this box takes 2–3s — and short enough
that a start stuck on a server that never answers does not queue every other
terminal behind its 120s, which is exactly what it did: one bad server name
held the lock for two minutes while two terminals that then started in 2s each
waited behind it. The waiters keep that clock, not the holder: `initialize()`
holds the GIL for its whole wait, so a timer thread meant to hand the lock on
early only ran after the call returned.

**Dates near the unix epoch.** The package converts datetimes through the
platform's local-time functions, and Windows probes a day either side to resolve
DST. Anything within about a day of the epoch pushes that probe below zero,
where Windows answers `EINVAL` — surfacing as the memorable
`SystemError: <built-in function history_deals_get> returned a result with an
exception set`. `mt5api/timeutil.py` therefore floors a full history pull at
1971-01-01.

**Logins do not fit in an INTEGER.** MT5 account numbers run to ten digits, past
2^31, so `MT5Account.login` is a `BigInteger`.

**History arrives after `login()` does.** On a funded account
`history_deals_total` reads 0 for the first seconds and then jumps - measured at
0 on return and 188 two seconds later. A sync reading through that window
reconstructs an empty account, and the ledger then derives `BalanceInit` from a
balance with no deals to explain it, so the damage is quietly wrong numbers
rather than a failure. `fetch.wait_for_history` blocks until the count
settles, and refuses to believe a zero on an account that holds money.

**A naive datetime handed to MT5 is read in *this machine's* timezone.** Every
datetime the package is given is reduced to a unix epoch and compared straight
against the bar and deal stamps, which are server wall-clock times dressed as
UTC epochs. A naive one gets there through the local-time conversion of
whichever machine made the call: on a machine on Eastern European time, asking
`copy_rates_range` for 12:00 returned bars stamped 09:00 in summer and 10:00 in
winter — the machine's own offset both times, the same for every broker,
because the broker never came into it.

That is easy to misread as the *server* reinterpreting the request, and the fix
that follows from the misreading — widen by the measured server offset, trim
back by bar time — only worked because on that machine the two offsets happened
to be the same number. It also missed the second half of the shift entirely:
inside the worker, position times are naive server labels, so `.timestamp()`
applied the machine's offset a *second* time and MAE/MFE was priced from bars
three hours before the position ever traded, on every position measured during
a sync.

`timeutil.as_mt5_time` tags the argument UTC, which makes the package's
conversion the identity. The window then comes back exactly as asked for — no
widening, no offset needed, and the same answer on any machine.

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

Needs a **Windows environment** — `MetaTrader5` is a Windows-only wheel, and so
is everything the terminal does. That environment can be Windows itself, or
Wine on Linux; the code is identical either way, and the Linux route is written
up in *Deploying on Linux* below. The repository only carries the service; the
four things below live outside it and have to exist on the box before the
service is any use.

### 1. Machine

* **Python 3.12, 64-bit.** The `MetaTrader5` wheel is CPython-specific and
  64-bit only, to match `terminal64.exe`.
* **PostgreSQL.** Any recent version. Create the role and database the
  connection string expects:

  ```sql
  CREATE ROLE mt5_api LOGIN PASSWORD '...';
  CREATE DATABASE mt5_api OWNER mt5_api;
  ```

  Tables are created at startup (`SQLModel.metadata.create_all`) and a column
  missing from an existing table is added in place (`db/session.py`). There are
  no migrations beyond that, so a change to an existing column's type or an
  enum needs the database dropped rather than altered.

### 2. The master terminal

Install MetaTrader 5 once, anywhere, then keep one cleaned copy as the master —
say `D:\MT5\master`. Every instance is a *portable* install: its data lives
beside `terminal64.exe` rather than in `%APPDATA%\MetaQuotes`, which is what
makes a clone a directory copy and keeps instances from sharing state.

Open it with `deploy\open-master.cmd`, which passes `/portable`. Opening it any
other way sends it to `%APPDATA%\MetaQuotes`, and the brokers added there will
never reach the clones.

Brokers do **not** have to be added to the master. The terminal looks a server
up by name through MetaQuotes' directory on the first login: measured on a
99-account list spanning 51 servers, none of which the master had seen, 50
resolved on the first try and the one that did not (`HFMarketsGlobal-Live3`)
timed out the same way a misspelt name does. *File → Open an Account* in the
master is only worth doing for a server the directory does not list — and then
close the terminal fully before cloning, since `Config\servers.dat` is written
on exit.

Keep the master on the current build. Every clone otherwise downloads the
update itself on first start (370 MB into `%APPDATA%\MetaQuotes`, portable or
not) and applies it on the next; opening the master, letting it download, and
opening it once more takes two minutes and every clone then starts current.

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

`-Root` is optional on every script in `deploy\`. Left out, it is read back from
`MT5_API_TERMINAL_PATHS` in `.env` — the clones are listed there and the master
sits beside them — falling back to `D:\MT5`. That is what makes
`open-master.cmd` work on a double-click, which passes no arguments at all.

### 4. The service

```powershell
pip install -r requirements.txt
copy .env.example .env      # then edit it
python -m orchestrator.app
```

The settings that decide whether it works at all:

| setting | note |
|---|---|
| `MT5_API_TERMINAL_PATHS` | the number of paths *is* the pool size, and each path string is the key an account is pinned to — rename one and its accounts are re-pinned elsewhere |
| `MT5_API_TERMINAL_PORTABLE` | must match how the instances were installed; a mismatch sends the terminals to a data directory with no accounts in it |
| `MT5_API_DATABASE_URL` | |
| `MT5_API_API_TOKEN` | leaving it empty disables auth on every route but `/healthz` |
| `MT5_API_PRUNE_CACHE_AFTER_SYNC` | on by default; without it the price cache grows without bound |

Startup logs an `app.startup.config` warning for each terminal path that is not
there, an empty pool, and a missing token. Then check `GET /healthz` and
`GET /pool/status`: one worker per path, all healthy.

### 5. Housekeeping

The price cache prunes itself. Each worker drops its own instance's price
history after every sync it finishes — `MT5_API_PRUNE_CACHE_AFTER_SYNC`, on by
default. Nothing is kept: what a later sync genuinely needs it fetches again,
and that is little, because MAE/MFE is measured once per position and the clock
is held in memory between syncs. Measured across four instances, this held the
pool at ~250 MB where it had reached 4.3 GB unattended.

It runs after the result is on its way, so a hard sync never waits for it, and
a failure is logged rather than failing the task. Files the terminal holds open
are skipped by Windows and go on the next run.

`deploy\prune-history.ps1` is what is left for the cases the service cannot
reach: an instance the pool is not currently syncing through, a one-off reclaim
with the pool stopped, or a dry run — without `-Apply` — to see the size
first.

### What it costs

Measured in a load test on one box (12 cores, 15 GB, four terminals, 99 real
accounts of which 66 had valid credentials):

| | |
|---|---|
| terminal after a sync, cache pruned | ~250 MB RSS; 1.3 GB peak for four |
| API + four worker processes | ~600 MB |
| CPU | peaks of 87% on cold starts and first-time MAE/MFE; steady background syncing barely registers |
| initial sync, warm terminal | p50 10 s, p90 28 s; up to 160 s for an account with thousands of positions |
| hard sync after the first, in the worker | p50 6 s |
| background cycle, 64 accounts | 4 min 19 s — 4.0 s per account across the pool |
| disk, per instance | 270 MB, plus the price cache the prune keeps in check |

At 4 s per account, a four-terminal pool re-syncs about **220 accounts inside a
15-minute interval** and about 890 inside an hour, once their history has been
priced. Two things eat into that: a new account costs 10–70 s of a worker on top,
and an account whose server never answers costs one 120 s. Neither disturbed
the rest of the pool in the test — no worker restarts, no terminals reset.

A cold terminal start is two to three seconds once the process is up, and
starts are staggered across the pool, so a pool has every terminal up within a
minute of tasks arriving — a terminal cannot start before a task supplies
credentials.

**Database connections.** A request holds a pooled connection only while it
reads or writes; a hard sync and an account registration give theirs back
before they wait on the pool. `MT5_API_DB_POOL_SIZE` (30) and
`MT5_API_DB_MAX_OVERFLOW` (50) size the pool — 80 in total, enough for fifty
hard syncs arriving at once with room to spare. Postgres itself defaults to
100 connections; the dev compose file raises it to 200, and a production
database must allow at least the pool's 80 plus everything else that connects. Before the wait was taken out of the session, sixteen
callers waiting on hard syncs used up a 15-connection pool and the worker that
had to write the result got none: the sync ran, the caller got 200 with the
data, and the database never saw it.

## Deploying on Linux

The service runs on Linux unchanged. `MetaTrader5` is imported in exactly one
place — `mt5api/terminal.py` — so rather than splitting the service in two and
bridging it over RPC, the whole process runs inside a single Wine prefix:
the API, the scheduler and every worker. Process spawning and the worker pipes
behave under Wine as they do on Windows, so the pool needs no changes at all.

Postgres stays a native Linux service, or a container. It has no business
inside the emulation, and keeping it out means it does not compete for the one
resource that actually runs short.

Measured on Ubuntu 24.04, 6 cores, 11 GB, against 100 real accounts: 63 active,
zero failures across sync storms of 20 and 50 concurrent, a background cycle of
6.3 s per account. The same work costs roughly twice the CPU it does on
Windows — 72% of the time was spent in the kernel, one terminal peaking at
three cores — because every Windows system call is being translated. Compare
the price per hundred accounts served, not the price of the box.

```bash
sudo bash deploy/linux/install.sh          # Wine, Xvfb, Windows Python, deps
docker compose -f deploy/linux/docker-compose.yml up -d    # or the distro's postgres
# put a current portable master at /opt/mt5/wine/drive_c/MT5/master
bash deploy/linux/clone-pool.sh 12
# write /opt/mt5/wine/drive_c/app/.env  (paths look like C:\MT5\t1\terminal64.exe)
sudo bash deploy/linux/service.sh install
python3 deploy/linux/verify-pool.py --token "$TOKEN" --parallel 12
```

The master is the deployment artefact, and it has to be on the current build.
A terminal from an older package downloads its update on the first login —
about 190 MB — and that download starves the deal-history fetch, so that first
sync fails with "no deal history arrived" while reporting the balance
perfectly correctly. Every clone repeats it, and on a bare box there are no
accounts yet to warm the pool with, so this cannot be fixed after the fact.

A terminal that has been in service has already solved both halves: it applied
the update on its next start, and it accumulated the broker list.
`make-master.sh` promotes one to master and strips what must not travel with
it — above all `Config/accounts.dat`, which holds the credentials of every
account that terminal ever logged into:

```bash
systemctl stop mt5api
bash deploy/linux/make-master.sh t1
```

Measured: a pool cloned from a current master starts on build 6182 and pulls
no update on login. `clone-pool.sh` refuses to clone a master that still
carries credentials.

One thing to know when testing a fresh pool: use accounts you have not been
logging in all day. A cold pool was checked with the same five accounts a dozen
times over one morning, each time from a freshly cloned terminal, and by the
end every one of them failed "no deal history arrived" at 30 s, at 90 s and at
150 s — while five other valid accounts, untouched that day, synced first time
through the very same terminals with history in 3–5 s. Repeated logins from a
string of new terminal identities make brokers withhold history; that is the
test, not the pool. The worker still retries such a sync once, with a longer
settle window, which covers the genuine slow cases.

Four things about this are not obvious, and each of them fails silently:

| | |
|---|---|
| `WINEDLLOVERRIDES="mscoree,mshtml="` | without it `wineboot` blocks forever on the Mono/Gecko dialog, which nothing can answer on a headless box. This is the usual reason a Wine setup looks hung |
| a virtual display | the terminals will not start without one, so `xvfb.service` exists and `mt5api.service` requires it |
| the interpreter is a *Windows* Python inside the prefix | a Linux `python3` cannot load the `MetaTrader5` wheel however much Wine is installed |
| the launch command lives in `/opt/mt5/bin/mt5api-run.sh` | systemd strips backslashes out of `ExecStart`, mangling every Windows path handed to it |

Pool size was measured rather than guessed, with one storm over the same 63
accounts:

| pool | storm | median sync | memory | free |
|---|---|---|---|---|
| 6 | 218 s | 46.2 s | 3.9 GB | 8.1 GB |
| 12 | 157 s | 33.2 s | 5.7 GB | 6.3 GB |
| 18 | 168 s | 31.9 s | 7.4 GB | 4.6 GB |
| 24 | 167 s | 32.6 s | 9.3 GB | 0.9 GB |

Everything is won going from 6 to 12; 18 and 24 are flat. Extra terminals pay
only while they fill the gaps where one waits on a broker, and once those are
full the work is back to sharing six cores. Roughly two terminals per core is
the ceiling. Twenty-four also leaves under a gigabyte free with no swap
configured, so the first unusual burst takes the service down rather than
slowing it.

`verify-pool.py` runs two rounds of hard syncs over every active account and
exits non-zero if the last one still fails, so it can gate a deploy.

Two Linux-specific settings worth raising in `.env`: importing the worker under
Wine costs ~20 s per process, so twelve workers spawning at once do not all
report within the default 120 s start window; set
`MT5_API_WORKER_START_TIMEOUT_SECONDS=300`. The pool's reaper restarts any that
missed it, one a minute, so the service still fills up — just slower.

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
   busy, each terminal's queue draining, and a hard sync fired mid-way coming
   back first on its own terminal.
4. Kill one `terminal64.exe` mid-sync — the service should restart that worker
   and finish the task on it, never on another.
5. Check the clock: `GET /accounts/{id}` should report a
   `server_utc_offset_minutes` that matches the terminal's own Market Watch
   time. Inside the trading week that comes off a live quote; at the weekend it
   falls back to the weekend scan, so either is a fair time to look. Then check
   positions from either side of a daylight saving switch — `ClosedAt` minus
   `ClosedAtUtc` should be three hours in summer and two in winter on a broker
   keeping European time. Every `*Utc` field carries a `Z` and no unsuffixed
   one does; a hard sync response and a later `GET` of the same position must
   agree on both.
