# Working on Preview AI gateway

Run `python -m pytest -q` for offline provider, security and SQLite tests. Keep credentials, runtime state and private configuration outside the repository. Visitor keys are held in short-lived server memory. Changes to task capabilities, origin handling, provider contracts or accounting require focused tests and review. Provider calls and deployment require the operator's authorization. The default registry remains empty; each application owns its task adapter.
