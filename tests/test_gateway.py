from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import uuid
import threading
import asyncio
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field

from gateway.app import COOKIE, EXHAUSTED_MESSAGE, create_app
from gateway.config import Config
from gateway.provider import MODEL, reservation_ns
from gateway.tasks import Project, Registry, Task

GATEWAY = "https://ai.example.test"
PREVIEW = "https://preview.example.test"
FAKE_KEY = "sk-test-" + "s" * 45
NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc).timestamp()


class TextInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1, max_length=2000)


def fixture_registry(config, *, parser=str, output_tokens=128):
    return Registry(config, [Project("fixture", PREVIEW, "https://github.com/latintzar/disdat")],
                    [Task("fixture.summary", "fixture", TextInput, "Summarize the supplied text.",
                          lambda value: value.text, parser, max_output_tokens=output_tokens)])


def response(*, usage=True, status="completed", model=MODEL, text="OFFLINE_FIXTURE"):
    result = {"id": "resp_fixture", "model": model, "service_tier": "default", "status": status,
              "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}
    if usage:
        result["usage"] = {"input_tokens": 30, "output_tokens": 10, "total_tokens": 40}
    return httpx.Response(200, json=result)


@pytest.fixture
def configured(tmp_path):
    clock = [NOW]
    config = Config(ledger_path=tmp_path / "private" / "ledger.sqlite3")
    return config, clock


def make_client(config, clock, handler, *, registry=None):
    app = create_app(config, registry or fixture_registry(config), transport=httpx.MockTransport(handler), clock=lambda: clock[0], session_clock=lambda: clock[0])
    return app, TestClient(app, base_url=GATEWAY)


def bootstrap(client, origin=PREVIEW):
    result = client.post("/api/session", headers={"Origin": origin}, json={})
    assert result.status_code == 200, result.text
    return result.json()["csrf"]


def request(client, csrf, *, mode="shared", task="fixture.summary", origin=PREVIEW, extra=None, request_id=None, text="Fixture text"):
    value = {"task_id": task, "request_id": request_id or str(uuid.uuid4()), "mode": mode, "input": {"text": text}}
    value.update(extra or {})
    return client.post("/api/run", headers={"Origin": origin, "X-CSRF-Token": csrf}, json=value)


def store_key(client, csrf, value=FAKE_KEY):
    result = client.post("/api/key", headers={"Origin": GATEWAY, "X-CSRF-Token": csrf}, json={"key": value})
    assert result.status_code == 200, result.text


def test_default_registry_rejects_unknown_and_shared_default_cannot_spend(configured):
    config, clock = configured; calls = []
    app = create_app(config, Registry(config), transport=httpx.MockTransport(lambda r: calls.append(r) or response()), clock=lambda: clock[0])
    with TestClient(app, base_url=GATEWAY) as client:
        csrf = bootstrap(client, GATEWAY)
        assert request(client, csrf, origin=GATEWAY).status_code == 404
        assert client.get("/health").json() == {"status": "ok", "shared_enabled": False, "registered_tasks": 0}
    app, client = make_client(config, clock, lambda r: calls.append(r) or response())
    with client:
        csrf = bootstrap(client)
        result = request(client, csrf)
        assert result.status_code == 503 and result.json()["code"] == "shared_disabled"
        assert not calls


def test_cookie_origin_csrf_tls_and_dedicated_password_popup(configured):
    config, clock = configured
    app, client = make_client(config, clock, lambda _: response())
    with client:
        assert client.post("/api/session", json={}).status_code == 403
        assert client.post("/api/session", headers={"Origin": "https://evil.example.test"}, json={}).status_code == 403
        result = client.post("/api/session", headers={"Origin": PREVIEW}, json={})
        cookie = result.headers["set-cookie"].lower()
        assert all(value in cookie for value in ["secure", "httponly", "samesite=strict", "path=/"])
        assert "domain=" not in cookie
        csrf = result.json()["csrf"]
        assert request(client, "bad", mode="own").status_code == 403
        assert request(client, csrf, mode="own", origin="https://evil.example.test").status_code == 403
        assert client.post("/api/key", headers={"Origin": PREVIEW, "X-CSRF-Token": csrf}, json={"key": FAKE_KEY}).status_code == 403
        popup = client.get("/key?project=fixture")
        assert popup.status_code == 200 and "type='password'" in popup.text
        assert "frame-ancestors 'none'" in popup.headers["content-security-policy"]
        assert popup.headers["cross-origin-opener-policy"] == "same-origin"
        assert popup.headers["cache-control"] == "no-store"
        assert client.get("/key?project=unknown").status_code == 404
    with TestClient(app, base_url="http://ai.example.test") as insecure:
        assert insecure.post("/api/key", json={"key": FAKE_KEY}).status_code == 400


def test_visitor_key_memory_only_expiry_delete_and_own_mode_zero_host_cost(configured, caplog):
    config, clock = configured; calls = []
    app, client = make_client(config, clock, lambda r: calls.append(r) or response())
    with client:
        csrf = bootstrap(client); store_key(client, csrf)
        result = request(client, csrf, mode="own")
        assert result.status_code == 200 and result.json()["result"] == "OFFLINE_FIXTURE"
        assert calls[0].headers["authorization"] == "Bearer " + FAKE_KEY
        status = app.state.ledger.status(0, clock[0])
        assert status["held_ns"] == status["settled_today_ns"] == 0
        assert FAKE_KEY not in result.text + caplog.text
        for path in config.ledger_path.parent.iterdir():
            assert FAKE_KEY.encode() not in path.read_bytes()
        clock[0] += 601
        assert request(client, csrf, mode="own").json()["code"] == "key_required"
        assert len(calls) == 1
        store_key(client, csrf)
        assert client.delete("/api/key", headers={"Origin": GATEWAY, "X-CSRF-Token": csrf}).json() == {"stored": False}
        assert request(client, csrf, mode="own").status_code == 401


def test_shared_receipt_settles_and_global_budget_exhaustion_copy(configured):
    config, clock = configured; calls = []
    held = reservation_ns(128)
    config = replace(config, shared_enabled=True, shared_key=FAKE_KEY, daily_limit_ns=held)
    app, client = make_client(config, clock, lambda r: calls.append(r) or response())
    with client:
        csrf = bootstrap(client)
        assert request(client, csrf).status_code == 200
        state = app.state.ledger.status(held, clock[0])
        assert state["held_ns"] == 0 and state["settled_today_ns"] == 30 * 1000 + 10 * 6000
        exhausted = request(client, csrf)
        assert exhausted.status_code == 429 and exhausted.json()["message"] == EXHAUSTED_MESSAGE
        assert [item["label"] for item in exhausted.json()["actions"]] == ["Use my key", "Get the code"]
        assert len(calls) == 1
        visitor = "sk-test-" + "v" * 45
        store_key(client, csrf, visitor)
        assert request(client, csrf, mode="own").status_code == 200
        assert calls[-1].headers["authorization"] == "Bearer " + visitor
        assert app.state.ledger.status(held, clock[0])["settled_today_ns"] == state["settled_today_ns"]


@pytest.mark.parametrize("case", ["timeout", "missing_usage", "wrong_model", "over_output", "bad_total", "boolean_total", "float_total", "huge_body", "http_error"])
def test_uncertain_provider_keeps_hold_and_hides_payload(configured, caplog, case):
    config, clock = configured; held = reservation_ns(128)
    config = replace(config, shared_enabled=True, shared_key=FAKE_KEY, daily_limit_ns=held)
    def handler(request):
        if case == "timeout": raise httpx.ReadTimeout("sensitive " + FAKE_KEY)
        if case == "missing_usage": return response(usage=False)
        if case == "wrong_model": return response(model="unregistered-model")
        if case == "huge_body": return httpx.Response(200, headers={"Content-Type": "application/json"}, content=b"x" * 65537)
        if case == "http_error": return httpx.Response(401, json={"error": FAKE_KEY})
        payload = json.loads(response().content)
        if case == "over_output": payload["usage"]["output_tokens"] = 10000
        if case == "bad_total": payload["usage"]["total_tokens"] = 0
        if case == "boolean_total": payload["usage"] = {"input_tokens": 0, "output_tokens": 1, "total_tokens": True}
        if case == "float_total": payload["usage"]["total_tokens"] = 40.0
        return httpx.Response(200, json=payload)
    app, client = make_client(config, clock, handler)
    with client:
        csrf = bootstrap(client)
        result = request(client, csrf)
        assert result.status_code == 502 and result.json()["code"] == "provider_uncertain"
        assert FAKE_KEY not in result.text + caplog.text
        state = app.state.ledger.status(held, clock[0]); assert state["held_ns"] == held and state["settled_today_ns"] == 0
        clock[0] += 86400
        restarted = create_app(config, fixture_registry(config), transport=httpx.MockTransport(handler), clock=lambda: clock[0])
        assert restarted.state.ledger.status(held, clock[0])["held_ns"] == held


def test_provider_contract_and_duplicate_request_never_resubmits(configured):
    config, clock = configured; calls = []
    app, client = make_client(config, clock, lambda r: calls.append(r) or response())
    with client:
        csrf = bootstrap(client); store_key(client, csrf); nonce = str(uuid.uuid4())
        assert request(client, csrf, mode="own", request_id=nonce).status_code == 200
        assert request(client, csrf, mode="own", request_id=nonce).status_code == 409
        assert len(calls) == 1
        sent = calls[0]; payload = json.loads(sent.content)
        assert str(sent.url) == "https://api.openai.com/v1/responses"
        assert payload["model"] == MODEL and payload["service_tier"] == "default"
        assert payload["store"] is False and payload["stream"] is False and payload["tools"] == []
        assert payload["reasoning"] == {"effort": "none"} and payload["max_output_tokens"] == 128
        assert "previous_response_id" not in payload and "conversation" not in payload


@pytest.mark.parametrize("extra", [{"model": "other"}, {"endpoint": "https://evil.test"}, {"instructions": "other"}, {"max_output_tokens": 9999}, {"key": FAKE_KEY}])
def test_client_cannot_override_provider_task_or_bounds(configured, extra):
    config, clock = configured; calls = []
    app, client = make_client(config, clock, lambda r: calls.append(r) or response())
    with client:
        csrf = bootstrap(client)
        result = request(client, csrf, extra=extra)
        assert result.status_code == 422 and FAKE_KEY not in result.text and not calls


def test_body_shape_bounds_and_validation_do_not_echo_key(configured, caplog):
    config, clock = configured
    app, client = make_client(config, clock, lambda _: response())
    with client:
        csrf = bootstrap(client)
        for value in [{"key": FAKE_KEY, "extra": "bad"}, {"key": FAKE_KEY + " invalid"}]:
            result = client.post("/api/key", headers={"Origin": GATEWAY, "X-CSRF-Token": csrf}, json=value)
            assert result.status_code == 422 and FAKE_KEY not in result.text
        result = request(client, csrf, text="x" * 2001)
        assert result.status_code == 422
        result = client.post("/api/run", headers={"Origin": PREVIEW, "X-CSRF-Token": csrf, "Content-Type": "application/json"}, content=b"x" * 16385)
        assert result.status_code == 413
        assert FAKE_KEY not in caplog.text


def test_incomplete_or_invalid_feature_output_settles_receipt_but_does_not_claim_success(configured):
    config, clock = configured
    config = replace(config, shared_enabled=True, shared_key=FAKE_KEY, daily_limit_ns=2 * reservation_ns(128))
    for handler, parser in [(lambda _: response(status="incomplete"), str),
                            (lambda _: response(), lambda text: (_ for _ in ()).throw(ValueError("private output")))]:
        app, client = make_client(config, clock, handler, registry=fixture_registry(config, parser=parser))
        with client:
            result = request(client, bootstrap(client))
            assert result.status_code == 502 and result.json()["code"] == "output_unavailable"
            assert app.state.ledger.status(config.daily_limit_ns, clock[0])["held_ns"] == 0


def test_expired_price_review_stops_shared_mode_before_provider(configured):
    config, clock = configured; clock[0] += 8 * 86400; calls = []
    config = replace(config, shared_enabled=True, shared_key=FAKE_KEY, daily_limit_ns=reservation_ns(128))
    app, client = make_client(config, clock, lambda r: calls.append(r) or response())
    with client:
        result = request(client, bootstrap(client))
        assert result.status_code == 503 and result.json()["code"] == "rate_review_required" and not calls


def test_config_reads_only_atelier_namespace_and_masks_key(configured):
    config, _ = configured
    safe = Config.from_env({"OPENAI_API_KEY": FAKE_KEY, "DAILY_LIMIT_USD": "999", "ATELIER_LEDGER_PATH": str(config.ledger_path)})
    assert not safe.shared_enabled and not safe.shared_key and safe.daily_limit_ns == 2_000_000_000
    assert FAKE_KEY not in repr(replace(safe, shared_key=FAKE_KEY))


def test_operator_ceiling_cannot_exceed_approved_two_dollars(configured):
    config, _ = configured
    with pytest.raises(ValueError):
        replace(config, daily_limit_ns=2_000_000_001)
    with pytest.raises(ValueError):
        Config.from_env({"ATELIER_DAILY_LIMIT_USD": "2.01"})


def test_idle_key_pruning_and_restart_erase_session_keys(configured):
    config, clock = configured
    app, client = make_client(config, clock, lambda _: response())
    with client:
        csrf = bootstrap(client); store_key(client, csrf)
        token = client.cookies.get(COOKIE); saved = app.state.sessions.get(token)
        assert saved.key
        clock[0] += 601
        app.state.sessions.prune()
        assert not saved.key
        store_key(client, csrf)
        restarted = create_app(config, fixture_registry(config), transport=httpx.MockTransport(lambda _: response()))
        from gateway.sessions import SessionError
        with pytest.raises(SessionError):
            restarted.state.sessions.get(token)


def test_two_project_sessions_share_one_atomic_request_budget(configured):
    config, clock = configured
    held = reservation_ns(128)
    config = replace(config, shared_enabled=True, shared_key=FAKE_KEY, daily_limit_ns=held)
    other = "https://other.example.test"
    registry = Registry(config, [Project("first", PREVIEW, "https://github.com/latintzar/disdat"),
                                 Project("second", other, "https://github.com/latintzar/leo")],
                        [Task("first.summary", "first", TextInput, "Summarize text.", lambda value: value.text, max_output_tokens=128),
                         Task("second.summary", "second", TextInput, "Summarize text.", lambda value: value.text, max_output_tokens=128)])
    entered, release = threading.Event(), threading.Event(); calls = []
    async def handler(value):
        calls.append(value); entered.set()
        assert await asyncio.to_thread(release.wait, 10)
        return response()
    app, client = make_client(config, clock, handler, registry=registry)
    with client:
        first_csrf = bootstrap(client); first_cookie = client.cookies.get(COOKIE)
        client.cookies.clear(); second_csrf = bootstrap(client, other); second_cookie = client.cookies.get(COOKIE)
        def call(task, source, token, csrf):
            return client.post("/api/run", headers={"Origin": source, "X-CSRF-Token": csrf, "Cookie": COOKIE + "=" + token},
                               json={"task_id": task, "request_id": str(uuid.uuid4()), "mode": "shared", "input": {"text": "Fixture"}})
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(call, "first.summary", PREVIEW, first_cookie, first_csrf)
            assert entered.wait(5)
            try:
                second = pool.submit(call, "second.summary", other, second_cookie, second_csrf).result(5)
                assert second.status_code == 429 and second.json()["message"] == EXHAUSTED_MESSAGE
                assert len(calls) == 1
                assert app.state.ledger.status(held, clock[0])["held_ns"] == held
            finally:
                release.set()
            assert first.result(5).status_code == 200


def test_cors_preflight_is_exact_and_credentials_are_scoped(configured):
    config, clock = configured
    app, client = make_client(config, clock, lambda _: response())
    with client:
        good = client.options("/api/run", headers={"Origin": PREVIEW, "Access-Control-Request-Method": "POST",
                                                    "Access-Control-Request-Headers": "content-type,x-csrf-token"})
        assert good.status_code == 200 and good.headers["access-control-allow-origin"] == PREVIEW
        assert good.headers["access-control-allow-credentials"] == "true"
        bad = client.options("/api/run", headers={"Origin": "https://evil.example.test", "Access-Control-Request-Method": "POST"})
        assert bad.status_code == 400 and "access-control-allow-origin" not in bad.headers


@pytest.mark.parametrize("limits", [{"max_output_tokens": 16.5}, {"max_input_bytes": True}, {"max_input_bytes": 4096.0}])
def test_registry_requires_exact_integer_request_bounds(configured, limits):
    config, _ = configured
    with pytest.raises(ValueError):
        Registry(config, [Project("fixture", PREVIEW, "https://github.com/latintzar/disdat")],
                 [Task("fixture.summary", "fixture", TextInput, "Summarize text.", lambda value: value.text, **limits)])


def test_ip_rate_history_survives_restart_with_disposable_session_secrets(configured):
    from gateway.ledger import LedgerError
    config, clock = configured
    app = create_app(config, fixture_registry(config))
    first = app.state.sessions
    ledger = app.state.ledger

    def admit(sessions, number):
        return ledger.admit("fixture", "fixture.summary", "own",
                            sessions.digest("offline-session-" + str(number)),
                            sessions.ip_digest("192.0.2.1"), str(uuid.uuid4()),
                            0, config.daily_limit_ns, clock[0])

    for number in range(8):
        ticket = admit(first, number)
        ledger.release_unsent(ticket, clock[0])
    with pytest.raises(LedgerError, match="ip_rate_limit"):
        admit(first, 8)
    old_ip, old_session = first.ip_digest("192.0.2.1"), first.digest("session")
    first.close()
    restarted = create_app(config, fixture_registry(config))
    assert restarted.state.sessions.ip_digest("192.0.2.1") == old_ip
    assert restarted.state.sessions.digest("session") != old_session
    with pytest.raises(LedgerError, match="ip_rate_limit"):
        admit(restarted.state.sessions, 9)
    assert ledger.status(config.daily_limit_ns, clock[0])["committed_ns"] == 0
    restarted.state.sessions.close()


@pytest.mark.parametrize("unsafe", ["public_mode", "symlink", "hardlink", "truncated"])
def test_ip_salt_refuses_unsafe_or_partial_state(configured, unsafe):
    import os
    from stat import S_IMODE
    config, _ = configured
    app = create_app(config, fixture_registry(config))
    app.state.sessions.close()
    salt = config.ledger_path.with_name(config.ledger_path.name + ".ip-salt")
    assert S_IMODE(salt.stat().st_mode) == 0o600
    assert S_IMODE(salt.parent.stat().st_mode) == 0o700
    assert salt.stat().st_size == 32
    if unsafe == "public_mode":
        salt.chmod(0o644)
    elif unsafe == "symlink":
        original = salt.with_suffix(".original")
        salt.rename(original)
        salt.symlink_to(original)
    elif unsafe == "hardlink":
        os.link(salt, salt.with_suffix(".copy"))
    else:
        salt.write_bytes(b"partial")
    with pytest.raises(ValueError, match="private owned"):
        create_app(config, fixture_registry(config))


def test_ip_salt_first_creation_is_shared_by_independent_instances(configured):
    from gateway.ledger import Ledger
    from gateway.sessions import Sessions
    config, clock = configured
    Ledger(config.ledger_path)

    def digest(_):
        sessions = Sessions(config, lambda: clock[0])
        result = sessions.ip_digest("192.0.2.1")
        sessions.close()
        return result

    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(digest, range(16)))
    assert len(set(results)) == 1


def test_registry_accepts_other_github_owners_and_rejects_invalid_links(configured):
    config, _ = configured
    registry = Registry(config, [Project("fixture", PREVIEW, "https://github.com/example-org/project")])
    assert registry.projects["fixture"].code_url == "https://github.com/example-org/project"
    for url in ("https://github.com/-bad/project", "https://github.com/example-org/project/extra",
                "https://github.com.evil.test/example-org/project", "https://github.com/example-org/project?next=x"):
        with pytest.raises(ValueError):
            Registry(config, [Project("fixture", PREVIEW, url)])
