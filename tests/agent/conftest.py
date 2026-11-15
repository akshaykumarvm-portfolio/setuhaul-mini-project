"""Agent tests: a scripted fake model drives the REAL agent graph (middleware + tools) against the
REAL booking service (in-process TestClient on a fresh test database). No LLM calls, no Upstash."""
import fakeredis
import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field
from langchain_core.messages import AIMessage

from agent.booking_client import BookingClient
from agent.checkpointer import RedisSaver
from agent.graph import build_agent
from agent.intake import Intake


class ScriptedModel(BaseChatModel):
    """Returns the scripted messages in order; accepts bind_tools like a real chat model.
    (A list + position, not an iterator: agent construction copies the model, which would share/consume one.)"""
    script: list = Field(default_factory=list)
    pos: list = Field(default_factory=lambda: [0])

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        if self.pos[0] >= len(self.script):
            raise AssertionError(f"script exhausted after {self.pos[0]} model calls")
        msg = self.script[self.pos[0]]
        self.pos[0] += 1
        return ChatResult(generations=[ChatGeneration(message=msg.model_copy())])


def ai(text: str = "", *calls: tuple[str, dict]) -> AIMessage:
    return AIMessage(content=text, tool_calls=[{"name": n, "args": a, "id": f"call_{n}_{i}"}
                                               for i, (n, a) in enumerate(calls)])


@pytest.fixture()
def booking_api(db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_url)
    from booking.api import app
    with TestClient(app) as c:
        c.post("/admin/clock", json={"sim_ts": "2026-08-04T10:00:00+05:30", "rate": 0})
        yield c


@pytest.fixture()
def client_factory(booking_api):
    def make(driver_id: str) -> BookingClient:
        bc = BookingClient(driver_id)
        bc.http = booking_api
        bc.http.headers["X-Driver-Id"] = driver_id
        return bc
    return make


@pytest.fixture()
def fake_redis():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture()
def make_intake(client_factory, fake_redis):
    def make(script: list[AIMessage]) -> tuple[Intake, ScriptedModel]:
        model = ScriptedModel(script=script)
        saver = RedisSaver(fakeredis.FakeRedis(decode_responses=False))
        agent = build_agent(checkpointer=saver, model=model, fallback_model=False, summarizer=model)
        # no extraction model: tests pass facts/eta_hint explicitly where needed
        return Intake(agent, redis_client=fake_redis, extraction_model=None, client_factory=client_factory), model
    return make
