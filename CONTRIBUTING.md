# Contributing

```sh
python -m pip install --constraint constraints.txt -e '.[dev]'
python -m unittest discover -s tests -q
ruff check gatewatch integrations scripts tests
ruff format --check gatewatch integrations scripts tests
python scripts/check_docs.py
```

See the [extension contracts](docs/extensions.md) before changing a plugin interface. Run installer and PAM checks in a disposable Debian or Ubuntu environment with the dependencies listed in the [CI workflow](.github/workflows/checks.yml).

Use synthetic addresses, usernames and logs in tests and screenshots. Keep tokens, raw configs, captured credentials and private chat details out of reports and patches.
