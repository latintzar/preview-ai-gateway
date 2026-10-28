---
name: preview-ai-gateway
description: Set up or integrate an operator-owned Preview AI gateway and reviewed typed application tasks.
---

Read the repository README and docs/integration.md, docs/hosting.md, docs/security.md and docs/provider-contract.md. Choose the operator's provider account, dedicated HTTPS gateway origin, same-site application origin and private ledger. Keep keys and configuration outside source; use an owned mode-0600 environment file and mode-0700 ledger directory. Run the offline tests and doctor. The default registry is empty: add and test a narrowly scoped typed task in the application's own factory, using the public ERP adapter linked in the integration guide as an example. Review current official provider pricing and the rate-card expiry before enabling shared spending. Configure TLS, exact origin/CORS and CSRF protections, and verify the deployed feature. Obtain authorization before provider calls or deployment. Do not print or store secrets in chat, source or browser storage.
