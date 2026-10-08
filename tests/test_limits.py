"""PR-05: per-IP rate limit and daily request cap."""

from datetime import date

from starlette.requests import Request

from dwarpal.limits import DailyCap, RateLimiter, client_ip
from tests.conftest import chat


def fake_request(host: str, forwarded: str | None = None) -> Request:
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
    return Request({"type": "http", "headers": headers, "client": (host, 1234)})


def test_rate_limiter_window_slides():
    now = [0.0]
    limiter = RateLimiter(2, clock=lambda: now[0])
    assert limiter.allow("a") and limiter.allow("a")
    assert not limiter.allow("a")
    assert limiter.allow("b")  # per key
    now[0] = 60.0
    assert limiter.allow("a")


def test_rate_limiter_zero_means_off():
    limiter = RateLimiter(0)
    assert all(limiter.allow("a") for _ in range(1000))


def test_daily_cap_resets_on_a_new_day():
    day = [date(2026, 10, 8)]
    cap = DailyCap(2, today=lambda: day[0])
    assert cap.allow() and cap.allow()
    assert not cap.allow()
    day[0] = date(2026, 10, 9)
    assert cap.allow()


def test_client_ip_uses_last_forwarded_hop_only_when_trusted():
    request = fake_request("10.0.0.1", forwarded="6.6.6.6, 203.0.113.7")
    assert client_ip(request, trust_forwarded_for=True) == "203.0.113.7"
    assert client_ip(request, trust_forwarded_for=False) == "10.0.0.1"
    assert client_ip(fake_request("127.0.0.1"), trust_forwarded_for=True) == "127.0.0.1"


def test_proxy_returns_429_over_the_rate_limit(make_client):
    client = make_client(rate_limit_per_minute=2)
    assert chat(client, "hi").status_code == 200
    assert chat(client, "hi").status_code == 200
    blocked = chat(client, "hi")
    assert blocked.status_code == 429
    assert blocked.json()["error"]["type"] == "rate_limit_error"


def test_proxy_returns_429_over_the_daily_cap(make_client):
    client = make_client(daily_request_cap=1)
    assert chat(client, "hi").status_code == 200
    blocked = chat(client, "hi")
    assert blocked.status_code == 429
    assert "daily" in blocked.json()["error"]["message"]


def test_limits_off_by_default(make_client):
    client = make_client()
    assert all(chat(client, "hi").status_code == 200 for _ in range(30))
