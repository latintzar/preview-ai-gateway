# Verification

Prepared 28 October 2026. All provider responses in these checks are mocked; live provider calls: **0**.

`python -m pytest -q`: **82 passed** on Python 3.12.3. Tests cover threaded/process SQLite admission, one allowance across projects, restart/midnight holds, deduplication, request/resource limits, exact origin/CSRF/cookie rules, account selection, visitor-key expiry/deletion, strict provider receipts, uncertain outcomes, private operator files and masked diagnostics.

Six additional regressions verify stable IP-rate history across restart, disposable session secrets, concurrent first creation of the private salt, and rejection of unsafe or partial salt files. The full offline suite passed after this fix; the existing browser proof below predates the salt-only change.

`python tests/browser_popup.py --output /tmp/atelier-gateway-proof`: passed in bundled Playwright 1.58.0 Chromium. It loads the actual client module, fills the dedicated password window with a synthetic key and executes the visitor-key path after a simulated exhausted shared budget. One mocked provider request uses that visitor key; the shared $2 hold is unchanged and own-key host cost is zero. The key field clears, deletion erases the session buffer, cookies are HttpOnly/Secure/SameSiteStrict, and keys stay out of browser storage, URLs, console and SQLite. All other network is blocked.

Screenshots at 1600 and 390 pixels were inspected for the password window and budget fallback. The test is an explicitly labelled offline fixture; it is not a deployed product preview. HTTPS is simulated through Playwright route interception into the real ASGI application. This proves the browser/application contract, rather than a certificate or reverse-proxy configuration.

For each deployment: commission an owned OpenAI project key, register and review each real feature adapter, deploy behind the dedicated same-site HTTPS gateway, verify proxy/cookie/CORS/popup behaviour there, and check account entitlement and actual provider usage receipts within the approved budget. Project quality, audio/image/tool workflows and business writes require their own feature integration. The default empty task registry remains intact.
