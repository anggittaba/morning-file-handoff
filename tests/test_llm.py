import json

import pytest

from handoff import db, explain, llm

from conftest import DAY, at


class FakeProvider:
    name = "fake:model"

    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, []

    def complete(self, system, user, schema, name):
        self.calls.append((system, json.loads(user), name))
        if self.error:
            raise self.error
        return self.reply


async def failed_row(env):
    await env.engine.run_pass(at(7, 30))  # BR03 has no file: missing at the deadline
    return db.get(env.con, DAY, "BR03")


async def test_llm_text_is_used_and_rule_category_kept(env):
    p = FakeProvider({"explanation": "No file came.", "likely_cause": "Export did not run.",
                      "next_step": "Call the branch."})
    row = await failed_row(env)
    env.con.execute("DELETE FROM explanations")
    exp = await explain.ensure(env.con, p, DAY, row, "Station Square", at(7, 31), at(7, 30))
    assert exp["engine"] == "fake:model" and exp["explanation"] == "No file came."
    assert exp["rule_category"] == "missing"
    facts = p.calls[0][1]
    assert facts["rule_category_hint"] == "missing" and facts["deadline"] == "07:30"


@pytest.mark.parametrize("provider", [
    FakeProvider(error=TimeoutError("slow")),
    FakeProvider({"explanation": "", "likely_cause": "x", "next_step": "y"}),
    FakeProvider({"something": "else"}),
])
async def test_bad_llm_output_falls_back_to_rules(env, provider):
    row = await failed_row(env)
    env.con.execute("DELETE FROM explanations")
    exp = await explain.ensure(env.con, provider, DAY, row, "Station Square", at(7, 31), at(7, 30))
    assert exp["engine"] == "rules" and exp["llm_error"]
    assert exp["explanation"].startswith("No export file")


async def test_explanation_written_once_per_failure(env):
    p = FakeProvider({"explanation": "a", "likely_cause": "b", "next_step": "c"})
    row = await failed_row(env)
    await explain.ensure(env.con, p, DAY, row, "x", at(7, 31), at(7, 30))
    await explain.ensure(env.con, p, DAY, row, "x", at(7, 32), at(7, 30))
    assert len(p.calls) <= 1


async def test_summary_draft_never_sends(env):
    await env.engine.run_pass(at(7, 30))
    d = db.latest_draft(env.con, DAY)
    assert d["engine"] == "template" and "0 of 24 branches delivered" in d["text"]
    p = FakeProvider({"summary": "Short update."})
    d2 = await explain.draft_summary(env.con, p, DAY, env.engine.names(), at(7, 31), at(7, 30))
    assert d2["text"] == "Short update." and d2["engine"] == "fake:model"


def test_provider_from_env(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("HANDOFF_LLM", raising=False)
    assert llm.provider_from_env() is None
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    assert isinstance(llm.provider_from_env(), llm.GeminiProvider)
    monkeypatch.setenv("HANDOFF_LLM", "off")
    assert llm.provider_from_env() is None


def test_openai_request_shape():
    seen = {}

    def post(url, payload, headers):
        seen.update(url=url, payload=payload, headers=headers)
        return {"choices": [{"message": {"content": json.dumps({"explanation": "a", "likely_cause": "b",
                                                                 "next_step": "c"})}}]}

    out = llm.OpenAIProvider("k", "m", post=post).complete("sys", "{}", llm.EXPLAIN_SCHEMA, "explain")
    assert out["explanation"] == "a"
    assert seen["payload"]["response_format"]["json_schema"]["strict"] is True
    assert seen["headers"]["Authorization"] == "Bearer k"


def test_gemini_request_shape():
    seen = {}

    def post(url, payload, headers):
        seen.update(url=url, payload=payload, headers=headers)
        return {"candidates": [{"content": {"parts": [{"text": json.dumps({"summary": "s"})}]}}]}

    out = llm.GeminiProvider("k", "g", post=post).complete("sys", "{}", llm.SUMMARY_SCHEMA, "summary")
    assert out == {"summary": "s"}
    assert seen["payload"]["generationConfig"]["responseSchema"]["type"] == "OBJECT"
    assert seen["headers"]["x-goog-api-key"] == "k"
