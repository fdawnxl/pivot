from pivot.credentials import ProviderCredential
from pivot.llm.client import LiteLLMClient


def test_llm_uses_lower_priority_provider_after_failure() -> None:
    calls = []

    def complete(**kwargs):
        calls.append(kwargs["model"])
        if len(calls) == 1:
            raise RuntimeError("offline")
        return {"choices": [{"message": {"content": "ok"}}]}

    client = LiteLLMClient(
        "primary-model",
        completion=complete,
        fallbacks=(ProviderCredential("backup", "backup-model"),),
    )
    assert client.complete([])["choices"][0]["message"]["content"] == "ok"
    assert calls == ["primary-model", "backup-model"]
