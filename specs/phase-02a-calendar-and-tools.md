# Phase 2a — Mock Calendar and Tools

**Status:** Complete
**Depends on:** Phase 1 — Backend Skeleton
**Supersedes:** nothing
**Source:** `specs/roadmap.md` Phase 2a

## Purpose

Give the agent something real to call. This phase builds the calendar layer
and the tool layer that sits on top of it, with no LLM anywhere. The point is
that every calendar operation is a plain Python call with a typed contract, a
logged audit trail, and a failure mode that can be simulated on demand — so
Phase 2b only has to add the conversation loop, not any calendar logic.

Nothing here talks to a network. The mock and the tools are exercised
directly from tests.

## Scope

### In scope

- `CalendarClient` — the interface the tools are allowed to call.
- `MockCalendarClient` — the implementation, on top of the existing SQLite
  database and `create_booking()`.
- Failure injection: `timeout`, `server_error`, `slot_taken_at_last_moment`.
- Tool functions with Pydantic input models and clean error results.
- A decorator that logs every tool call to `tool_calls`.
- `handoff_to_human` tool.
- `confirmed=True` enforced in code for every write tool.

### Out of scope

- Any HTTP route for the calendar. The tools are called in-process.
- The conversation loop, the LLM, tool-call dispatch by the model.
- The FAQ knowledge base and `search_knowledge` (Phase 2c).
- Voice, STT/TTS, the browser call screen (Phase 3).
- SMS confirmation. Phase 1 flagged that a masked number cannot receive one.

## Design decisions

### The interface, not the mock

Tools call `CalendarClient` and never `MockCalendarClient`. The interface is
the seam Phase 6 uses: a Cal.com or Google Calendar client is written against
the same four methods and substituted, with no change to tool or agent code.

`CalendarClient` is an abstract base class, not a `Protocol`. A real provider
has real shared state (an HTTP session, a token) and wants a place to hang
helpers; an ABC gives it one, and makes the contract explicit in the source
rather than inferred from usage.

### Failure injection lives in the mock, never in tool arguments

Tool input models are handed to the LLM verbatim in Phase 2b. A
`failure_mode` field on a tool input would put "make this call time out" in
front of the model, where a caller could talk it into it. The failure plan is
therefore configured on the client, never on the tool.

The mock accepts a plan at construction or through `set_failure()`:

```python
mock.set_failure("book_slot", "timeout", times=2)   # next two calls fail
mock.set_failure("check_availability", "server_error")  # always fails
```

- `operation` — `check_availability`, `book_slot`, `reschedule`, `cancel`, or
  `*` for all four.
- `mode` — `timeout`, `server_error`, `slot_taken_at_last_moment`.
- `times` — `None` means "always"; an integer means "fail the next N calls,
  then succeed". This is how retry behaviour is tested.

`slot_taken_at_last_moment` is only valid for `book_slot`. It models the race
where the slot was free a moment ago and is now gone: `check_availability`
still reports it as free, and the booking fails. Applying it to any other
operation is a configuration error and raises `ValueError`.

### Injected failures raise the exceptions a real client would raise

The mock raises `CalendarTimeoutError` and `CalendarProviderError`, the same
types a real HTTP client raises on a read timeout and on a 5xx. The tools
handle mock failures and real failures with one code path, so Phase 6 does not
introduce a second error-handling path that was never exercised.

`slot_taken_at_last_moment` raises `CalendarSlotUnavailableError` — the same
error a genuine double-booking produces, so the tool's "that slot was just
taken" reply is identical either way.

### Time is UTC everywhere except at the edges

Business hours, slot length and timezone are configuration. The business
timezone is the only place local time exists:

- The tool layer receives a business-local date and time from the LLM and
  converts to UTC before calling the client.
- The client works entirely in UTC, and stores UTC.
- The tool layer converts back to business-local time for the reply.

A clinic in New York and a caller in another timezone agree on the same
instant; only the display differs.

### `confirmed=True` is enforced in code

Every write tool takes a `confirmed: bool`. The tool refuses to write unless
it is `True`. The LLM setting the flag is the point: it has to assert
explicitly that the caller agreed, and a caller talking the model into
skipping the confirmation is visible in the logs as a `confirmation_required`
refusal rather than a silent booking.

`handoff_to_human` carries the flag too. Escalating to a human is a
deliberate action and should be as explicit as a write, even though the
caller never agreed to it — the *agent* is the one confirming, deliberately.

The flag is a tripwire, not a proof. It makes "the agent said it confirmed"
an observable event, not an invisible one.

### Logging is a decorator

Tool functions are wrapped by `@tool(...)`. The wrapper validates the input,
times the call, masks phone numbers, writes the `tool_calls` row, and turns
any exception into a clean error result. A new tool cannot forget to log,
because logging is not in the tool body.

### Injected failures are marked in the log

`tool_calls` gains an `injected` column. Evaluation needs to tell an injected
failure from a real bug — a Phase 5 report that counts provider errors must
not count the ones the test suite asked for.

## Data contract

### Schema change

`tool_calls` gains one column:

```sql
injected INTEGER NOT NULL DEFAULT 0 CHECK (injected IN (0, 1))
```

`SCHEMA_VERSION` moves from 1 to 2. No migration tooling: the dev database is
disposable and is rebuilt with `python scripts/init_db.py --reset`.

No other table changes. The nine-table set is unchanged, so `/api/v1/health`
keeps passing.

### `bookings` and cancelled-then-rebooked

`create_booking()` gains one behaviour: when the idempotency key repeats but
the existing row is **cancelled**, the row is revived — status back to
`confirmed` — rather than returned as-is.

Without this, a caller who books 10:00, cancels, and books 10:00 again would
get back the cancelled booking. The key is derived from the caller and the
slot, so a repeat with a cancelled row means exactly that case. A repeat with
a *confirmed* row still returns the original, unchanged — that is the retry
guarantee from Phase 1.

## Calendar interface

```python
class CalendarClient(ABC):
    def check_availability(self, window_start: datetime, window_end: datetime) -> list[Slot]
    def book_slot(self, *, caller_name, caller_number_masked,
                  slot_start, slot_end, idempotency_key) -> Booking
    def reschedule(self, *, booking_id, slot_start, slot_end) -> Booking
    def cancel(self, *, booking_id) -> Booking
```

- `window_start` / `window_end` are UTC. The mock generates the business-hour
  slot grid inside the window and marks each slot free or busy.
- `idempotency_key` is passed in, not derived by the client. The client only
  ever sees the *masked* number, and a masked number cannot tell two callers
  apart, so the key must be computed upstream from the raw number. The key is
  a hash — the raw number never reaches the database.
- `reschedule` and `cancel` take a `booking_id`. Resolving a caller's booking
  from a name and number is the *tool's* job, not the client's, so the
  interface stays provider-shaped.

### Exceptions

| exception | meaning | tool error code |
|---|---|---|
| `CalendarTimeoutError` | provider did not answer in time | `timeout` |
| `CalendarProviderError` | provider failed | `provider_error` |
| `CalendarSlotUnavailableError` | slot is no longer free | `slot_unavailable` |
| `CalendarNotFoundError` | no such booking | `not_found` |

All inherit `CalendarError`, which carries an `injected: bool` attribute.

## Tool contract

Every tool is `(conn, InputModel) -> payload`. The decorator wraps it into a
`ToolResult`, which is returned as a plain dict.

```json
{
  "ok": true,
  "tool": "book_slot",
  "message": "Booked for Ada on 2026-11-02 at 10:00.",
  "data": { "...": "..." }
}
```

```json
{
  "ok": false,
  "tool": "book_slot",
  "error_code": "timeout",
  "message": "The booking system did not respond. I'd like to try that again.",
  "data": {}
}
```

`error_code` is absent on success; `message` on success is the spoken reply,
which the tool's own payload supplies. On failure, `message` is written for a
person hearing it over the phone and never carries exception text, stack
detail, or provider internals.

The first argument is a `sqlite3.Connection`. The decorator does not close a
connection the caller supplied; when the caller passes `None`, the decorator
opens one and closes it.

Error codes: `invalid_arguments`, `confirmation_required`, `timeout`,
`provider_error`, `slot_unavailable`, `not_found`, `internal_error`.

### Tools

| tool | input | write | notes |
|---|---|---|---|
| `check_availability` | `date` | no | business-local `YYYY-MM-DD`; returns free slots as local `HH:MM` |
| `book_slot` | `caller_name`, `caller_number`, `date`, `time`, `confirmed` | yes | `time` is business-local `HH:MM` |
| `reschedule` | `caller_name`, `caller_number`, `date?`, `time?`, `new_date`, `new_time`, `confirmed` | yes | current slot optional; most recent match is used |
| `cancel` | `caller_name`, `caller_number`, `date?`, `time?`, `confirmed` | yes | most recent match is used |
| `handoff_to_human` | `reason`, `summary`, `confirmed` | yes | writes a `handoffs` row |

`reason` is one of `complaint`, `urgent`, `sensitive`, `medical`, `unsure`,
`other`.

`handoff_to_human` writes its `handoffs` row with a null `call_id`; the agent
loop in Phase 2b supplies the real one via the decorator's `_call_id`
keyword.

No tool input model contains a failure-mode field, or any other field the
model could use to steer the system. This is asserted by a test over the JSON
schema of every registered tool.

### Idempotency key

```
bk_<sha256(caller_name | caller_number | date | time)[:32]>
```

The raw number goes into the hash and nowhere else. The stored
`caller_number_masked` is what reaches the database.

## Configuration

| variable | default | meaning |
|---|---|---|
| `VOICECHAT_CALENDAR_TIMEZONE` | `America/New_York` | business timezone |
| `VOICECHAT_CALENDAR_OPEN_TIME` | `09:00` | opening time, business-local |
| `VOICECHAT_CALENDAR_CLOSE_TIME` | `17:00` | closing time, business-local |
| `VOICECHAT_CALENDAR_SLOT_MINUTES` | `30` | slot length |
| `VOICECHAT_CALENDAR_CLOSED_WEEKDAYS` | `saturday,sunday` | closed days, by name |
| `VOICECHAT_CALENDAR_FAILURE_MODE` | *(unset)* | default failure mode, for manual demos |
| `VOICECHAT_CALENDAR_FAILURE_TIMES` | *(unset)* | how many calls fail; unset means always |

The new variables carry a `VOICECHAT_` prefix so they cannot collide with the
unprefixed Phase 1 variables, and so the failure variables are unmistakable
in a `.env`.

### The failure-mode guard

`VOICECHAT_CALENDAR_FAILURE_MODE` is for manual demos. Setting it in an
environment that is not `dev`, `development`, or `test` **refuses to start the
app** — a production deploy with a failure switch left on is a configuration
error, not a convenience.

The guard lives in `create_app()`, not in the mock, so it holds no matter how
the client is constructed.

## Database access

`db.log_tool_call()` writes one row per tool call: `call_id`, `tool_name`,
`arguments_json`, `result_json`, `success`, `latency_ms`, `injected`,
`created_at`.

`arguments_json` is the validated input, with the phone number replaced by
its masked form. `result_json` is the `ToolResult`. Both are stored as JSON
text.

## Acceptance criteria

- [x] `CalendarClient` is an interface; the tools never reference the mock.
- [x] `MockCalendarClient` implements `check_availability`, `book_slot`,
      `reschedule`, `cancel` against SQLite and `create_booking()`.
- [x] Free slots reflect confirmed bookings; a cancelled slot is free again.
- [x] `book_slot` is idempotent on the key; a cancelled booking can be
      rebooked with the same key.
- [x] `reschedule` keeps the booking id and frees the old slot.
- [x] `cancel` frees the slot and marks the booking cancelled.
- [x] Business hours, slot length and timezone come from config; stored times
      are UTC.
- [x] Failure injection: `timeout`, `server_error`, `slot_taken_at_last_moment`.
- [x] `times=N` fails the next N calls, then succeeds; `times=None` always
      fails.
- [x] Injected failures raise the same exception types a real client would.
- [x] Every tool call is logged with arguments, result and latency.
- [x] Phone numbers are masked in the log.
- [x] Injected failures are logged with `injected = 1`.
- [x] Every tool returns a clean error result instead of raising.
- [x] `confirmed=False` on a write tool refuses the write.
- [x] No tool's JSON schema contains a failure-mode field.
- [x] The app refuses to start with a failure mode set outside dev/test.
- [x] `handoff_to_human` writes a `handoffs` row.
- [x] `schema.sql` gains `tool_calls.injected`, with a test.
- [x] `pytest` green, `ruff` clean.

## Known limitations

- **Slot generation is Python-side.** The mock builds the slot grid from
  business hours rather than asking a provider. A real provider returns its
  own availability, so `check_availability` becomes a pass-through in Phase 6.
- **Availability filtering loads bookings in Python.** Times are compared as
  parsed datetimes, not as strings, so a booking row written with a
  different ISO precision cannot be missed. The volume is a clinic's, which
  is small; a real provider does this filtering server-side.
- **The idempotency key is derived, not supplied.** A caller who genuinely
  wants two separate bookings for the same person at the same slot cannot
  make one, which is the correct outcome for a receptionist.
- **No SMS confirmation.** A masked number cannot receive one. Phase 1
  flagged this; it is unchanged here.
- **Reschedule is last-write-wins.** Retrying a reschedule after a later,
  different reschedule reverts the booking. The booking id is stable and the
  audit trail is in `tool_calls`, so the history is recoverable, but the final
  state is not a function of the key alone.
