import pytest

from services import payping_affiliate as affiliate


def test_bridge_disabled_without_authenticated_https(monkeypatch):
    monkeypatch.delenv("PAYPING_BOT_API_KEY", raising=False)
    monkeypatch.setenv("PAYPING_API_BASE_URL", "https://payping.example.org")
    assert not affiliate.configured()
    monkeypatch.setenv("PAYPING_BOT_API_KEY", "test-private")
    monkeypatch.setenv("PAYPING_API_BASE_URL", "http://payping.example.org")
    assert not affiliate.configured()
    monkeypatch.setenv("PAYPING_API_BASE_URL", "https://payping.example.org")
    assert affiliate.configured()


@pytest.mark.asyncio
async def test_referral_join_requires_distinct_user_and_uses_matching_code(monkeypatch):
    events = []

    async def capture(action, payload):
        events.append((action, payload))
        return {"joined": True, "newReferral": True}

    monkeypatch.setattr(affiliate, "_send", capture)
    with pytest.raises(affiliate.AffiliateBridgeError):
        await affiliate.record_join(12345, 12345)
    assert events == []
    assert (await affiliate.record_join(22222, 11111))["newReferral"]
    assert events == [("join", {"telegramUserId": "22222", "referralCode": "a11111"})]


@pytest.mark.asyncio
async def test_registration_and_pairing_only_call_scoped_backend(monkeypatch):
    events = []

    async def capture(action, payload):
        events.append((action, payload))
        return {"referralCode": "a11111", "linked": True}

    monkeypatch.setattr(affiliate, "_send", capture)
    await affiliate.register(11111, "hello_world")
    await affiliate.link_account(11111, "hello_world", "ABCD1234")
    assert events == [
        ("register", {"telegramUserId": "11111", "telegramUsername": "hello_world"}),
        ("link", {"telegramUserId": "11111", "telegramUsername": "hello_world", "code": "ABCD1234"}),
    ]
