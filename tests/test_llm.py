import json

import httpx
import pytest

from quant.llm import explain_with_model
from quant.strategy import analyze


def test_unconfigured_model_is_explicit_local_fallback(monkeypatch):
    monkeypatch.delenv("QUANT_LLM_BASE_URL", raising=False)
    signal = analyze("BTC-USDT-SWAP", {}, as_of_ms=1_800_000_000_000)
    assert explain_with_model(signal)["llm_status"] == "not_configured"


@pytest.mark.parametrize("bad", [False, True])
def test_model_cannot_introduce_facts_or_change_signal(monkeypatch, bad):
    monkeypatch.setenv("QUANT_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("QUANT_LLM_MODEL", "unit-test-model")
    monkeypatch.setenv("QUANT_LLM_API_KEY", "unit-test-key")
    signal = analyze("BTC-USDT-SWAP", {}, as_of_ms=1_800_000_000_000)
    original = json.dumps(signal, sort_keys=True)

    def handler(request):
        assert request.headers["Authorization"] == "Bearer unit-test-key"
        assert "tools" not in json.loads(request.content)
        content = {"ordered_fact_ids": ["guaranteed-profit-999"] if bad else ["F1", "F0"], "emphasis": "risk"}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content)}}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = explain_with_model(signal, client=client)
    assert result["llm_status"] == ("fallback" if bad else "success")
    assert "999" not in result["text"]
    assert json.dumps(signal, sort_keys=True) == original


def test_model_http_error_does_not_leak_credentials(monkeypatch):
    monkeypatch.setenv("QUANT_LLM_BASE_URL", "https://example.com/v1")
    monkeypatch.setenv("QUANT_LLM_MODEL", "unit-test-model")
    monkeypatch.setenv("QUANT_LLM_API_KEY", "unit-secret")
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401))) as client:
        result = explain_with_model(analyze("BTC-USDT-SWAP", {}, as_of_ms=1_800_000_000_000), client=client)
    assert result["llm_status"] == "fallback"
    assert "unit-secret" not in json.dumps(result)
