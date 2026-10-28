"""Offline Chromium proof. Routes simulate HTTPS; this is not a deployed TLS test.

Run from the checkout: python3 tests/browser_popup.py --output /tmp/atelier-gateway-proof
Requires Playwright 1.58.0 and its Chromium browser. All network is intercepted;
the provider uses httpx.MockTransport and only a synthetic visitor key is entered.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

from gateway.app import COOKIE, EXHAUSTED_MESSAGE, create_app
from gateway.config import Config
from test_gateway import GATEWAY, PREVIEW, NOW, fixture_registry, response


def run(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    visitor_key = "sk-test-" + "v" * 45
    host_key = "sk-test-" + "h" * 45
    calls = []
    with tempfile.TemporaryDirectory(prefix="atelier-popup-private-") as temporary:
        config = Config(ledger_path=Path(temporary) / "private" / "ledger.sqlite3",
                        shared_enabled=True, shared_key=host_key)

        def provider(request):
            # The only outgoing provider request must use the visitor's synthetic key.
            assert request.headers["authorization"] == "Bearer " + visitor_key
            calls.append({"host": request.url.host, "mode": "own"})
            return response()

        app = create_app(config, fixture_registry(config), transport=httpx.MockTransport(provider),
                         clock=lambda: NOW)
        # Simulate an already uncertain full-day hold, never an actual provider call.
        ticket = app.state.ledger.admit(project="fixture", task="fixture.summary", mode="hosted",
                                       session_hash="a" * 64, ip_hash="b" * 64,
                                       request_id=str(uuid.uuid4()), reserved_ns=2_000_000_000,
                                       daily_limit_ns=2_000_000_000, now=NOW - 121)
        app.state.ledger.submitted(ticket, now=NOW - 121)
        app.state.ledger.uncertain(ticket, now=NOW - 121)

        with TestClient(app, base_url=GATEWAY) as client, sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 1600, "height": 1000})
            blocked = []
            console = []
            urls = []

            def route_request(route):
                request = route.request
                urls.append(request.url)
                if request.url == PREVIEW + "/":
                    route.fulfill(status=200, content_type="text/html", body="""<!doctype html>
<html lang='en'><title>Offline gateway fixture</title>
<link rel='stylesheet' href='https://ai.example.test/static/client.css'><body>
<button id='run'>Run fixture</button><output id='result'></output>
<script type='module'>
import {PreviewAI} from 'https://ai.example.test/static/client.js';
const ai=new PreviewAI({gatewayOrigin:'https://ai.example.test',project:'fixture'});
document.querySelector('#run').onclick=async()=>{
  try{document.querySelector('#result').textContent=await ai.run({taskId:'fixture.summary',input:{text:'Fixture text'}});}
  catch(error){document.querySelector('#result').textContent=error.message;}
};</script></body></html>""")
                elif request.url.startswith(GATEWAY + "/"):
                    # TestClient must never lend its cookie jar to Chromium.
                    client.cookies.clear()
                    headers = {key: value for key, value in request.all_headers().items()
                               if key not in {"host", "content-length"}}
                    result = client.request(request.method, request.url, headers=headers,
                                            content=request.post_data_buffer)
                    response_headers = {key: value for key, value in result.headers.items()
                                        if key not in {"content-length", "content-encoding"}}
                    route.fulfill(status=result.status_code, headers=response_headers, body=result.content)
                else:
                    blocked.append(request.url)
                    route.abort()

            context.route("**/*", route_request)
            context.on("console", lambda message: console.append(message.text))
            page = context.new_page()
            page.goto(PREVIEW + "/")
            page.locator("#run").click()
            dialog = page.get_by_role("dialog")
            dialog.wait_for()
            assert EXHAUSTED_MESSAGE in dialog.inner_text()
            assert dialog.get_by_role("link", name="Get the code").get_attribute("href") == "https://github.com/latintzar/disdat"
            page.screenshot(path=str(output / "budget-fallback-1600.png"))
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path=str(output / "budget-fallback-390.png"))
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            with context.expect_page() as opened:
                dialog.get_by_role("button", name="Use my key").click()
            popup = opened.value
            popup.set_viewport_size({"width": 1600, "height": 1000})
            popup.wait_for_url(GATEWAY + "/key?project=fixture")
            popup.get_by_label("OpenAI API key").wait_for()
            assert popup.evaluate("window.opener === null")
            assert popup.locator("#key").get_attribute("type") == "password"
            popup.screenshot(path=str(output / "key-popup-1600.png"))
            popup.set_viewport_size({"width": 390, "height": 844})
            popup.screenshot(path=str(output / "key-popup-390.png"))
            assert popup.evaluate("document.documentElement.scrollWidth <= innerWidth")
            popup.get_by_label("OpenAI API key").fill(visitor_key)
            popup.get_by_role("button", name="Use my key", exact=True).click()
            popup.get_by_role("status").filter(has_text="Key ready.").wait_for()
            assert popup.locator("#key").input_value() == ""
            page.get_by_text("OFFLINE_FIXTURE", exact=True).wait_for(timeout=15_000)
            assert len(calls) == 1
            for current in (page, popup):
                assert current.evaluate("document.cookie") == ""
                assert current.evaluate("localStorage.length + sessionStorage.length") == 0
            cookies = context.cookies(GATEWAY)
            assert len(cookies) == 1 and cookies[0]["name"] == COOKIE
            assert cookies[0]["httpOnly"] and cookies[0]["secure"] and cookies[0]["sameSite"] == "Strict"
            assert visitor_key not in "".join(urls + console)
            assert host_key not in "".join(urls + console)
            for path in config.ledger_path.parent.iterdir():
                assert visitor_key.encode() not in path.read_bytes()
                assert host_key.encode() not in path.read_bytes()
            status = app.state.ledger.status(2_000_000_000, NOW)
            assert status["held_ns"] == 2_000_000_000 and status["settled_today_ns"] == 0
            assert not blocked
            popup.get_by_role("button", name="Forget my key").click()
            popup.get_by_role("status").filter(has_text="Key forgotten.").wait_for()
            assert all(not session.key for session in app.state.sessions._items.values())
            browser.close()
    receipt = {"kind": "offline-chromium-https-route-proof", "live_provider_calls": 0,
               "mock_provider_requests": len(calls), "host_budget_held_ns": 2_000_000_000,
               "own_key_host_cost_ns": 0, "cookie": "Secure; HttpOnly; SameSite=Strict; host-only",
               "key_storage": "short-lived server memory; cleared after Forget my key",
               "artifacts": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                             for path in sorted(output.glob("*.png"))},
               "limit": "TLS is simulated by browser route interception; actual reverse proxy/provider acceptance remains."}
    (output / "proof.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args().output)
