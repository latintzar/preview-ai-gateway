# Preview AI gateway

Preview AI gateway is a server for adding bounded AI tasks to public software previews. Each application registers its own typed task and result handler. The gateway can use an operator-funded daily allowance or a visitor’s own OpenAI key.

```text
Browser → registered typed task → atomic daily ledger → OpenAI
           visitor key ──────────────────────────────→ OpenAI
```

The default registry is empty: register a project and its reviewed tasks before enabling a feature. The shared allowance is disabled by default and is capped at $2 per UTC day across projects. Visitor keys enter in a separate same-site HTTPS window and stay briefly in server memory.

## Get started

Python 3.12+ is required.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m gateway.cli doctor
```

Copy `.env.example` to an owned private file outside the checkout, set its permissions to `0600`, and configure your own HTTPS origins and private ledger path. The example contains a blank provider key. See [hosting](docs/hosting.md) for TLS, proxy and configuration. The doctor checks configuration without calling the provider; its default registry reports zero projects and tasks.

Register tasks in application code. [Integration](docs/integration.md) describes the registry and links to a complete [AI Native ERP task and factory](https://github.com/latintzar/ai-native-erp/tree/86f87be80a98c86b60a91e3357e79980d063ca05/tools/preview-gateway). Run your own factory with the gateway package available on its Python path. The generic CLI serves the empty registry, while a project factory supplies its reviewed registry.

[Security](docs/security.md) covers the exact Origin, CORS, CSRF, cookie and private-key boundaries. [Architecture](docs/architecture.md) explains reservations and uncertain outcomes. [Provider contract](docs/provider-contract.md) records model prices reviewed on 28 October 2026; shared admission requires a current review. [Verification](docs/verification.md) describes offline tests and deployment checks.
