# ResyGrabber — local reservation automation

A safety-focused fork of [korbinschulz/resybot-open](https://github.com/korbinschulz/resybot-open),
retaining its terminal client, local server bridge, scheduled availability checks,
and **automatic reservation submission**. The original MIT license is preserved.

**Unofficial, experimental software.** It uses undocumented Resy interfaces that may
change or be restricted. There is no guarantee of inventory or successful checkout.
Review Resy's current terms before use; use only your own account for personal dining.

## What this fork does—and does not claim

- Direct single-date availability lookup, optional fixed proxy configuration, and
  identifiable restaurant/date progress logs.
- Bounded worker duration and cooperative stop controls. No worker starts on import.
- Persistent one-booking campaign claims across processes and restarts.
- Automatic submission only after configured charge ceilings and required quote
  fields pass. Success requires an exact match in Upcoming Reservations.
- Uncertain submissions remain held; they are never automatically retried.
- Cancellation records are removed only after account verification.
- A read-only dry-run mode; offline tests never contact Resy or make reservations.
- Local-only authenticated server, private file permissions, and redacted output.

The upstream README advertised CAPTCHA bypasses and proxy-based evasion. Those are
**not promises of this fork**. The existing bridge request methods have not been
expanded to develop new bypasses. The worker does not solve CAPTCHAs or payment/
identity challenges. Authentication/access/rate-limit errors stop the attempt.
The unused solver/account-generation and payment-setup UI has been removed; create
accounts and manage payment methods on the normal Resy website.

## Installation

Supported: **Python 3.10–3.13 on macOS or Linux**. Use an isolated environment, not
your system Python. Never reuse the upstream's formerly committed virtual environment.

```sh
git clone https://github.com/raymondyou97/resybot-open.git
cd resybot-open
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes --only-binary=:all: -r requirements.lock
python start.py
```

`start.py` launches the server and client using the same interpreter. Or use separate
terminals with the same virtual environment:

```sh
python server/server.py
python client/entry.py
```

The server listens **only on 127.0.0.1:8000**. A private local bearer token is generated
in `.state/.local-server-token`. The client and server must use the same checkout.
Restart **both** after upgrading. Do not expose the server through a tunnel or public
reverse proxy. Cloud-deployment templates have been removed.

## Existing users / migration

Your local JSON files are not deleted or migrated automatically. Default configuration
is `client/`, regardless of the launch directory. Set `RESY_DATA_DIR` in **both** server
and client environments to use another private directory.

- Reads/writes restrict configuration files to mode 0600. This is not encryption.
- Existing account aliases must stay stable and unique. Do not configure the same
  physical account under multiple aliases or rename an alias during a campaign.
- Legacy tasks remain readable, but need the new fee/terms configuration before live
  submission: **Show tasks → Configure fee limits/terms**.
- Existing solver keys in `info.json` are ignored, not displayed or sent anywhere.
- Schedules are now saved in `schedules.json`. Old in-memory schedules cannot be recovered.
- Never run the older client alongside the hardened version. One client per data
  directory is enforced; SQLite guards also protect cross-process claims.

## Running tasks

1. Add your own account in **Manage Accounts**. Credential entry is hidden and local.
2. Add a task with restaurant ID, party size, date range, hour window, polling delay,
   campaign group, and the charge/terms policy you reviewed in the official checkout.
3. First use **Dry run (no checkout)** to inspect available slots.
4. Use **Start Tasks (live)** and confirm the explicit live-booking prompt.

Start Tasks launches **all saved tasks**, up to four active worker threads. Review
and remove obsolete test tasks before enabling live booking.

```sh
# Offline date-format check; does not validate login or payment readiness:
python client/entry.py --check

# One read-only availability pass, bounded to 120 seconds by default:
python client/entry.py --dry-run --duration 120
```

Dry-run does not call the details or booking endpoints, create submission claims,
charge a card, or change existing reservations. It does not prove checkout readiness.
It still queries Resy and may encounter rate limits. A duration shorter than the full
scan can end before every requested date is checked.

### Time and polling

Ranges are inclusive, limited to 31 days. Past dates and expired same-day slots are
skipped; offset-aware slot times are normalized to the venue timezone. Hours remain the
original integer-hour convention: an end hour of 19 includes 19:00 through 19:59.
The selected slot must explicitly report the requested calendar date and venue.

The delay applies **between each date lookup**, with a minimum of one second—not
once per restaurant. Multiple tasks have independent workers, so their rates add up.
These defaults are not a claim about Resy's permitted request rate. Do not use
parallel tasks or proxy rotation to evade limits. HTTP failures stop without retry.

Schedules use **America/New_York**, persist across client restarts, and reference a
stable task identity instead of a mutable list index. Weekly means the selected
weekday, not every day. The client must remain running; missed one-time releases
are skipped rather than caught up. Cron/DST behavior follows APScheduler.

### Stop and duration

The deadline is measured while the worker runs, not after it finishes. Polling waits
are interruptible. Stop reports **stopping** while an HTTP request is still in flight,
and **stopped** only after the thread actually exits. A submitted request cannot be
recalled; its persistent hold remains until verification. Normal HTTP waits have
connect/read timeouts, but this is cooperative cancellation, not a hard process kill.

### Charge and terms safeguards

Live tasks require:

| Field | Meaning |
| --- | --- |
| `accept_terms` | `true` only after reviewing the venue's booking/cancellation terms |
| `currency` | Explicit three-letter currency, e.g. `USD` |
| `max_total_charge` | Maximum reported total charge for the reservation |
| `max_cancellation_fee` | Maximum cancellation exposure for the party |

The quote adapter requires explicit `payment.amounts.total`, quote currency, and
`cancellation.fee.amount`. Missing/ambiguous fields, nonfinite or negative amounts,
and currency/ceiling mismatches stop before submission. Cancellation amounts are
conservatively multiplied by party size; this may reject an acceptable per-reservation
fee rather than underestimate a per-person fee. Do not raise ceilings merely to get
past a rejection.

**The hardened quote adapter has synthetic offline coverage, not a verified live
production quote fixture.** Some upstream responses omit currency or amount fields;
those tasks will stop, even where the legacy bot would have submitted blindly. This
is a known compatibility limitation, not a reason to infer a zero charge. A new fee
or policy layout needs reviewed adapter support before unattended booking is safe.

### Success, duplicates, and unresolved holds

A campaign defaults to one restaurant; assign the same campaign ID to alternative
restaurants when only one should book. A claim blocks the same account's campaign
**or venue**. Claims are atomic across processes. The server consumes each submitted
claim once before dispatch, preventing a repeated local request from dispatching twice.

An HTTP 201 or a reservation ID alone is **not success**. The worker checks the exact
venue, date, clock time, and party size in Upcoming Reservations. A confirmed result
persists and suppresses later runs of that campaign. Other campaigns are unaffected.

Timeouts, crashes, stopped submissions, or missing confirmation retain a non-expiring
hold. Stop active workers, then use **Verify campaign holds**. A release requires a
successful account check, explicit confirmation that you also checked the normal
account/email, and no matching booking. Recent in-flight attempts and past-date
ambiguity remain held. **Do not delete the database to bypass a hold.**

External/manual bookings and clients using separate data directories are outside the
shared guard; an additional pre-submission account check reduces but cannot eliminate
those races. Historical successes from the old bot were not automatically imported.

### Cancellation

View Reservations refreshes the account and does not expose opaque reservation links
or account tokens. Cancellation requires explicit selection/confirmation and a charge
ceiling. The reservation is checked before the request and again afterward. A failed
request, uncertain result, or still-present reservation leaves the local record and
hold intact. The tool does not infer a free cancellation from absent policy data.

## Security and development

Read [SECURITY.md](SECURITY.md) and [CONTRIBUTING.md](CONTRIBUTING.md). Local configuration,
state, tokens, and logs are ignored. The pre-commit scanner checks staged files, known
local credential matches, and selected secret formats without printing matches.

```sh
python -m pip install --require-hashes --only-binary=:all: -r requirements-dev.lock
git config core.hooksPath .githooks
python -m unittest discover -s tests -v
ruff check client server scripts tests start.py
ruff format --check client server scripts tests start.py
python scripts/check_secrets.py
pip-audit --disable-pip --no-deps -r requirements.lock
```

GitHub Actions runs offline tests, lint/format, dependency consistency, advisory audits,
and secret checks. Never add real account credentials or live bookings to CI.

## Code map

- `client/resygrabber.py`: existing terminal workflow and configuration prompts.
- `client/task_executor.py`: bounded automatic worker and local bridge calls.
- `client/control.py`, `client/scheduling.py`: cancellation, deadlines, persisted scheduling.
- `client/booking_state.py`: atomic durable campaign claims and verification holds.
- `client/fees.py`, `client/verification.py`, `client/reservations.py`: charge limits,
  exact confirmation checks, and verified cancellation.
- `client/config_store.py`, `client/local_auth.py`: private storage and local authentication.
- `server/server.py`: authenticated loopback bridge, without response/credential logging.
- `tests/`: synthetic offline regression coverage; socket connections are prohibited.

MIT licensed. See [LICENSE](LICENSE) for the original attribution.
