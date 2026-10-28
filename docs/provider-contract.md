# Provider contract

Verified from official primary documentation on 28 October 2026. The offline test suite uses mocked provider responses.

| Field | Contract |
|---|---|
| Host/path | `https://api.openai.com/v1/responses` |
| Model snapshot | `gpt-5.4-mini-2026-03-17` |
| Input | Server instructions plus bounded text; no conversation/history/files/URLs/tools |
| Output | Task-owned 16–1024-token cap; `max_output_tokens` bounds visible and reasoning output |
| Processing | `service_tier='default'`, `reasoning.effort='none'`, `store=false`, nonstreaming |
| Verified standard text rates | $0.75 input / $4.50 output per million tokens |
| Budget tariff ceilings | $1 input / $6 output per million tokens; discounts ignored |
| Reserve | Full 400,000-token context plus task output ceiling at tariff ceilings |
| Receipt | Exact model/default tier, valid input/output/total counts within bounds; usage received on this request |
| Network bounds | No redirects, inherited proxy or retry; 40-second total deadline; 65,536-byte response limit |

The shared rate review is valid 28 October–4 November 2026. Shared admission fails closed afterwards until an operator verifies the primary sources and updates the reviewed code. A model name/rate/endpoint is never accepted from the browser.

Sources:

- [GPT-5.4 Mini model, snapshot, context, rates and reasoning controls](https://developers.openai.com/api/docs/models/gpt-5.4-mini)
- [Responses API creation contract](https://developers.openai.com/api/reference/resources/responses/methods/create)
- [Reasoning/output-token limits](https://developers.openai.com/api/docs/guides/reasoning)
- [API pricing and tool/region charges](https://developers.openai.com/api/docs/pricing)

`store=false` controls response storage; provider data handling follows the selected account's policies. Verify account entitlement, provider response acceptance, TLS hosting and real task quality for your deployment.
