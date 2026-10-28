# Architecture and decisions

The built-in allowance ceiling is $2 per UTC day. The default task registry is empty.

1. A real project calls a named task with typed input and a client request UUID. Its allowed origin, prompt, renderer, parser and output ceiling are registered in server code. The default registry is empty.
2. A secure, host-only, HttpOnly/SameSiteStrict cookie identifies a short-lived in-memory session. Mutations also require its CSRF token and an exact allowlisted HTTPS Origin.
3. The chosen account is explicit. Shared mode uses only `ATELIER_OPENAI_API_KEY`; own-key mode uses the visitor key held in that session and reserves zero host money. Errors preserve the selected account.
4. SQLite commits admission before an upstream request. `BEGIN IMMEDIATE` serializes budget, dedupe, request-rate and resource checks across independent processes. The ledger stores metadata and amounts, never keys, input or output.
5. A verified model/tier/token usage receipt replaces a hold with a conservative charge. Timeouts, malformed receipts and crashes retain their full holds. A short resource lease can expire while the monetary hold persists.
6. The adapter returns a parsed feature result. The consuming app owns its real feature callback and business behaviour. Model text grants no tools or business-service access.

## Money

The shared ceiling is at most **$2 per UTC day across every project and session**. Request data cannot change it. Integer nanodollars avoid floating-point budget errors. Configuration allows an operator to lower the ceiling; values above $2 are rejected.

Pending/uncertain holds from earlier days continue to count. Settled usage is charged on the receipt's UTC day. Restarts and midnight therefore preserve unresolved cost. Repeated request UUIDs in a session are rejected rather than resubmitted. Every active gateway process must use the same private ledger file and allowance configuration.

The adapter reserves the model's full documented 400,000-token context plus its task's output ceiling, priced at conservative ceilings of $1/$6 per million input/output tokens. A 512-output task reserves $0.403072, then releases unused capacity after a valid receipt. This deliberately trades small residual availability for a bound independent of guessed tokenization or message framing.

Actual token counts are settled at those conservative ceilings. This is gateway allowance accounting, rather than an OpenAI invoice. The standard rates verified on 28 October are lower. The shared rate card expires on 4 November pending re-verification; visitor-key mode retains its own account and request bounds.

## Resource bounds

Task input is at most 8,192 rendered UTF-8 bytes; HTTP input is at most 16,384 bytes with bounded nesting. Output is at most 1,024 tokens and a 65,536-byte provider/result envelope. Provider requests have a 40-second total deadline, explicit phase timeouts and no retries/redirects/proxy inheritance.

The ledger allows at most four active leases globally and one per session, twenty admissions/minute globally, four/session and eight/IP. A lease lasts 120 seconds; uncertain calls may occupy it until expiry. Admission and key/session control endpoints have separate memory/request bounds. External provider work may continue after a disconnected request; its monetary reservation remains held.

## Why a separate key origin

The password window belongs to the gateway origin, separate from every preview origin. Preview JavaScript receives presence/status only and cannot read the password window or retrieve a stored key. CSP prevents framing; COOP/noopener separates windows. The browser sends a host-only secure cookie, and explicit CORS allows only registered same-site origins.

This initial implementation supports one gateway process for its memory-only sessions and same-site HTTPS preview origins. Multiple hosts/replicas or cross-site embedding require a reviewed session architecture. SQLite itself is tested across processes; that does not make the in-memory key store distributed.
