# Test pass 2026-11: dibao service and Google Tasks integration

Scope: `service/dibao.py` endpoints, the `todo-actions.jsonl` queue (`service/todo_actions.py`), and the `todos`
bucketing in `collectors/build_home.py` / `collectors/collect_gtasks.py` (America/Los_Angeles, date-only `due`,
the DST change on 2026-11-01).

All new tests use fakes: a fake `gog` (`FakeGog` from `service/tests/test_todo_actions.py`), a fake collector runner
for `/v1/collect`, a temp git repo for home-data (`service/tests/_env.py`), and a stubbed `sync_down`.
No network calls, no credentials, no real task data. Every task ID and title in the tests is made up.

Command: `make test` (= `unittest discover` over `service/tests` and `collectors/test_*.py`).

## Results

| | service tests | collector tests | total |
|---|---|---|---|
| Before (baseline, `main` @ ba38338) | 102 OK | 55 OK | 157 |
| After | 117 OK | 64 OK | 181 |

The full suite was run 3 times in a row after the changes, with no flaky failures.

Line + branch coverage (`coverage run --branch`, both suites combined) on the files in scope:

| file | before | after |
|---|---|---|
| `service/dibao.py` | 78% | 79% |
| `service/todo_actions.py` | 95% | 96% |
| `service/digest.py` | 95% | 95% |
| `collectors/build_home.py` | 92% | 93% |
| `collectors/collect_gtasks.py` | 97% | 97% |
| **total** | **87%** | **88%** |

The existing suite already covered most lines, so line coverage barely moved. What the new tests add is
*behavior* coverage: failure modes, ordering, idempotency and time boundaries that ran before but were never asserted.
The code `dibao.py` still leaves uncovered is mostly process plumbing: `main()`, `setup_logging`, the background
loops in `start_threads`, `due_tick`, and `POST /v1/push/test`.

## New tests

`service/tests/test_service_pass.py` (15 tests)

- **Queue, dedupe by record ID**: the same pending first line appears 3 times in the file. `gog` runs once, one
  state line is written, and a rerun is a no-op.
- **Queue, retry when offline**: DNS / unreachable-network errors from `gog` are now treated as transient and get
  the 2/4/8 s backoff. 4xx and auth errors still fail immediately. A table of transient and permanent messages
  pins the classifier.
- **Queue, offline replay end to end**: with `git pull` failing and `gog` offline, the local queue is still executed,
  the failure is recorded and committed locally, and `/health` shows `last_sync_ok=false`. After recovery the
  failed action stays failed (it is not replayed automatically), and a new tick with a new ID succeeds.
- **Queue, lost state line**: an action recorded in the local ledger (`todo-actions-done.jsonl`) but missing its
  state line in home-data has that line re-appended, without calling `gog` again.
- **Queue, exception mid-batch**: one action raises an unexpected error (non-UTF-8 `gog` output). The other actions
  in the batch keep their state lines, and the bad one is marked `failed`.
- **`/health`**: `pending_todos` counts invalid-op lines until they are processed, `rebasing=true` makes
  `ok=false`, and the expected keys are present.
- **`POST /v1/todos/<id>/complete|undo`**: the response never echoes `title` (it stays only in the ledger).
  `already` returns 200. The same ID sent twice while another batch holds `todo_lock` returns 202 both times and
  writes one first line; after the lock is released, a poll executes it once. Offline `gog` returns 502 with the
  reason after 1 + 3 attempts. Invalid JSON and `from` values are rejected with 400 and nothing is written.
- **`POST /v1/collect` / `GET /v1/collect/<job>`**: only the named sources run (fake runner). A failing collector
  is reported in the job result and through `status.py fail <msg>`. `/health.collect_failures` increments.
  Validation returns 400 for a non-list `sources` or an unknown source, and 404 for an unknown job, with no
  collector started.
- **`POST /v1/edits`**: an oversize `Content-Length` returns 400 without reading the body, as does invalid JSON or
  `from=ios`.
- **Digest across DST**: "done today" uses the LA date (23:30 PST on 11-01 = 07:30Z on 11-02 counts for 11-01).
  The 08:00 morning slot on 11-01 is already PST (16:00Z, not 15:00Z), and the 30-minute grace edge is checked.

`collectors/test_todos_tz.py` (9 tests, property-style, fixed random seed)

- `la_today` is checked against an independent reference (-7 h / -8 h with the 2026 switch instants) every
  7 minutes across 5-day windows around both 2026 DST changes (03-08 and 11-01).
- Midnight edges: the second before and the instant of LA midnight around both changes. LA midnight is 07:00Z
  before 11-01 and 08:00Z after; a hard-coded -7 would put 11-02 07:30Z on the wrong day.
- The repeated 01:30 hour on 11-01 (08:30Z PDT, 09:30Z PST) maps to the same day. Any aware `now`, such as
  Tokyo time, works.
- Partition property over 400 random task sets × random "now" values in 10-30 … 11-04:
  - every task due on or before today lands in exactly one of `today` / `overdue`;
  - future tasks land in none;
  - `nodate` is a capped subset;
  - `overdue` is sorted;
  - `counts` are the real totals;
  - `due` is never shifted.
- Date-only `due`: `2026-11-01T00:00:00.000Z` (17:00 PDT on 10-31 as an instant) counts as "today" for the whole
  25-hour LA day of 11-01, checked every 10 minutes, and flips at 08:00Z on 11-02.
- `failed_todos`: the 24 h window is real elapsed hours across DST. Malformed or naive timestamps don't crash.

## Bugs found and fixed

Each fix is minimal. The `home.json` contract is unchanged: same keys, same shapes.

1. **Offline `gog` errors were not retried** (`service/todo_actions.py`). `TRANSIENT_RE` did not match the Go
   network errors `no such host`, `network is unreachable` or `no route to host`. A tick made while Wi-Fi or DNS was
   briefly down was marked `failed` on the first try, and failed actions are sticky. Fix: those three phrases were
   added to the transient pattern.
2. **Lost state lines left actions pending forever** (`service/dibao.py` `process_todos`). The local ledger is
   written *before* the state line is committed to home-data, so the action is not executed again. If the process
   died in between, or the commit was lost, `summarize` hid the action from the work list, but no state line was
   ever written. The web and iOS clients showed "pending" forever, and the digest and `todos.failed` never saw the
   outcome. Fix: each batch now re-appends the remembered state line for such actions (no `gog` call). There is a
   new helper, `todo_actions.local_lines()`, and `local_done()` now uses it.
3. **One unexpected exception lost a whole batch's state lines** (`service/dibao.py` `process_todos`). `_gog` only
   catches `TimeoutExpired` / `OSError`. Anything else, such as `UnicodeDecodeError` from `text=True` on non-UTF-8
   output, ended the batch after earlier actions had already run in Google Tasks and been remembered locally, so
   their state lines were never written (the same symptom as bug 2). Fix: each `execute` call is wrapped. An
   unexpected error marks only that action `failed` with `执行异常：<ExceptionType>` (no raw message, so nothing
   private leaks).
4. **A naive timestamp in `todo-actions.jsonl` crashed `build_home`** (`collectors/build_home.py` `failed_todos`).
   `now - ts` raised `TypeError` (aware minus naive), which took down the whole `home.json` rebuild. The file can be
   appended through GitHub by the clients or edited by hand. Fix: a naive timestamp is treated as UTC.
5. **A non-string `due` crashed the collector** (`collectors/collect_gtasks.py` `open_tasks`). Slicing an int raised
   `TypeError`. Fix: `str(...)` before slicing. Bad values become "no due date", as bad strings already did.

## Reviewed with no change needed

- The bucketing logic (`collect_gtasks.todos`) is correct. It compares date-only `due` (first 10 characters, never
  converted to another timezone) with `la_today()`, which uses `zoneinfo` and handles both DST changes. Buckets are
  computed at build time, so they roll over at LA midnight even if collection is failing.
- `digest.slot_day` / `todos_done_today` work correctly across DST. My first draft of the slot test was wrong, not
  the code: 08:00 on 11-01 is already PST.

## Remaining gaps (not addressed here)

- `start_threads` loops, `main()` / `--selftest`, `due_tick`, and `POST /v1/push/test` have no tests (process
  plumbing; APNs is already covered in `test_apns.py`).
- `build_home.load_readings` / `due_items` use the host's local date (`dt.date.today()`), not LA. This is fine on
  the Mac mini, whose system timezone is LA, but it is inconsistent with `todos`. I left it alone because it is out
  of scope and changing it would affect `readings.folded` and `due`.
- `app.js` (the web client's own "today" logic, if any) is not covered by these Python tests.
