# Contributing

Focused bug reports, tests, documentation corrections, and small patches are
welcome.

Participation is governed by the [Code of Conduct](CODE_OF_CONDUCT.md).

Do not put credentials, workspace or channel identifiers, private logs,
transcripts, manuscripts, or real Reading Packs in an issue or pull request.
Report a suspected vulnerability through the private process described in
[SECURITY.md](SECURITY.md).

## Development checks

Python 3.11 or newer is required. The default test path needs no external
service or API credential.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
sh scripts/test-suite.sh
```

Before submitting a change:

- keep platform-, provider-, and book-specific behavior behind their existing
  interfaces;
- add or update tests for behavior changes;
- update the operator documentation when configuration or deployment changes;
- use only the synthetic fixture in public tests;
- run `sh scripts/test-suite.sh` and resolve every failure.

The core package intentionally has no runtime dependency. Add an integration
dependency only when an optional adapter requires it, and update the reviewed
lock and hashes when it affects the Linux container.

## Licensing

By submitting a contribution, you agree to license it under the license shown
for its path in [the license map](LICENSES/README.md): MIT for code and CC BY
4.0 for documentation and examples.
