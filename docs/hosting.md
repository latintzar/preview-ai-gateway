# Host setup

Choose your own host, provider account, registered domain and dedicated gateway origin.

1. Use Python 3.12+ and a private virtual environment. Install `requirements.txt`; use `requirements-dev.txt` for tests.
2. Create a dedicated mode-0700 ledger directory outside the checkout. Put the operator configuration in an owned regular mode-0600 file outside source. `.env.example` lists the accepted names and built-in $2 ceiling. Its shared flag is false and key is blank.
3. Choose a dedicated HTTPS gateway origin and registered site domain. Every project preview origin must be a separate origin on that site. Register its real task adapters and GitHub link. The empty default registry rejects all feature calls.
4. Run `python -m gateway.cli doctor --env-file /private/atelier-preview.env`. Output contains flags/counts, never values or provider calls.
5. After configuring an owned key and reviewing registered tasks, the operator may enable shared mode within the built-in ceiling. Run `python -m gateway.cli serve --env-file /private/atelier-preview.env` behind the TLS proxy. It binds 127.0.0.1:8817, trusts forwarded metadata only from 127.0.0.1, disables access logs and caps incoming concurrency.

The proxy must overwrite forwarded headers, preserve the gateway Host and Origin, limit bodies to 16 KB, and disable body/header/query logging. The ASGI app rejects HTTP; set trusted proxy/TLS forwarding correctly. Keep the gateway inaccessible directly from the public network. Every project shares this one ledger and configuration; use one gateway worker for its memory-only sessions.

Run local mocked proofs before deployment. Then verify real HTTPS cookies/CORS, popup isolation, physical browser behaviour, selected personal provider account, and one actual feature per project. Use a project-specific factory to serve registered tasks; the generic CLI serves the empty registry.
