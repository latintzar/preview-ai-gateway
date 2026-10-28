# Real feature integration

Keep the project's original provider adapter. Add a thin preview-specific task boundary that preserves the real feature's request/result semantics. Register the task in server code before exposing its button.

Each `Project` declares one exact same-site HTTPS preview origin and its GitHub repository. Each `Task` declares an input model with `extra='forbid'`, static instructions, a bounded input renderer, a result parser and an output ceiling. Treat renderers/parsers as trusted, pure, bounded application code. The registry is immutable after app creation.

Default `gateway.tasks.default_registry` returns an empty registry. Unknown task IDs reject before provider work. Test fixtures are confined to `tests/`; they are not shipped as public product features.

The frontend imports `PreviewAI` from the gateway's `/static/client.js` and supplies the configured origin/project. A real button passes its reviewed `taskId` and typed input to `run`, then passes the result to its actual app callback. The helper offers the exact budget fallback, opens the isolated password window, and uses visitor-key mode after the visitor chooses it. It never reads the key or uses local/session storage. It makes no automatic retry after a provider-uncertain outcome.

Load `/static/client.css` from the gateway for the compact fallback dialog, or style its `.atelier-ai-fallback` class with the project's own reviewed tokens. Allow that exact gateway in the preview's `connect-src`, module `script-src` and stylesheet policy. The key-entry window keeps its own CSP and visual boundary.

## HTTP contract

- `POST /api/session` with JSON `{}` and a registered Origin creates/reads the secure session, returns its CSRF token and key-presence/expiry flags. The server cookie identifies it.
- `POST /api/key` accepts `{key: ...}` only from the gateway origin with cookie/CSRF. `DELETE /api/key` forgets it. These belong to the password window.
- `POST /api/run` accepts exactly `task_id`, `request_id` (UUID), `mode` (`shared`/`own`) and typed `input`. Cookie, CSRF and matching project Origin are required. Results contain parsed `result`, request UUID and selected mode.
- A budget rejection returns HTTP 429 with code `daily_budget_used`, the exact approved message and server-owned **Use my key**/**Get the code** actions. Shared-disabled and rate-review-required responses offer the same account/code choice without claiming funds were spent.
- A submitted request with uncertain provider outcome returns `provider_uncertain`. Its reservation remains held. The client exposes that uncertainty and leaves a fresh attempt to the user's choice.

The gateway currently supports text/typed-result feature tasks. Audio, image generation, long-running agents, provider tool execution and business-service mutations remain project-specific integration work. This infrastructure does not replace an ERP runtime with a generic completion or claim that deferred features work.

## Working example

The [AI Native ERP adapter and factory](https://github.com/latintzar/ai-native-erp/tree/86f87be80a98c86b60a91e3357e79980d063ca05/tools/preview-gateway) register a bounded customer task and connect the result to a real application feature. Configure your own origin and repository and review the task for your application. The gateway package must be installed or on the factory’s Python path.
