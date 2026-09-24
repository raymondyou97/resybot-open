# Contributing

Keep the client/server design and unattended booking intent, but fail closed on
unverified policies, charges, account results, and access challenges. Do not add
CAPTCHA bypasses, stealth, proxy rotation to evade limits, or live bookings to tests.

## Development

Use Python 3.10–3.13 on macOS/Linux and an isolated environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes --only-binary=:all: -r requirements-dev.lock
git config core.hooksPath .githooks
python -m unittest discover -s tests -v
ruff check client server scripts tests start.py
ruff format --check client server scripts tests start.py
python scripts/check_secrets.py
pip-audit --disable-pip --no-deps -r requirements.lock
pip-audit --disable-pip --no-deps -r requirements-dev.lock
```

Tests block real socket connections and use only temporary state and synthetic data.
Keep coverage for exactly-once dispatch, ambiguous submissions, duplicate claims,
charge limits, cancellation verification, stop/deadline races, and schedule persistence.
Never infer booking success from HTTP 201 or a reservation ID alone.

Edit `requirements.in` or `requirements-dev.in` and regenerate hash locks on Python
3.10 using pip-tools:

```sh
pip-compile --generate-hashes --no-emit-index-url --no-emit-trusted-host -o requirements.lock requirements.in
pip-compile --generate-hashes --no-emit-index-url --no-emit-trusted-host -o requirements-dev.lock requirements-dev.in
```

For workflow changes, validate YAML locally with Go installed:

```sh
go run github.com/rhysd/actionlint/cmd/actionlint@v1.7.7 .github/workflows/ci.yml
```

Test the resolved versions on the CI Python matrix before merging. Dependency pins
and a passing advisory scan are not a complete supply-chain review. Keep the original
MIT license and attribution. Submit changes as draft pull requests.
