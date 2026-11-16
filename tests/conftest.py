import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import fakeredis
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agent import Agent  # noqa: E402
from app.brain import Brain  # noqa: E402
from app.config import Settings  # noqa: E402
from app.crypto import generate_key  # noqa: E402
from app.db import make_session_factory  # noqa: E402
from app.ratelimit import Limiter  # noqa: E402
from app.telegram import Notifier  # noqa: E402


class FakeLLM:
    """Imita anthropic.Anthropic().messages.create com respostas configuráveis."""

    def __init__(self):
        self.classification = {"intent": "elogio", "language": "pt", "toxicity": 0.0,
                               "needs_reply": True, "confidence": 0.95, "summary": "elogio"}
        self.draft_text = "Obrigado! Ainda bem que gostaste das avaliações físicas."
        self.verification = {"grounded": True, "safe": True, "reason": "ok"}
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        tool = (kw.get("tool_choice") or {}).get("name")
        if tool == "classificar":
            return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name=tool, input=self.classification)])
        if tool == "verificar":
            return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name=tool, input=self.verification)])
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.draft_text)])


class RecordingNotifier(Notifier):
    def __init__(self):
        self.drafts, self.alerts = [], []

    def send_draft(self, draft):
        self.drafts.append(draft)
        return 1000 + draft.id

    def alert(self, text):
        self.alerts.append(text)


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, ig_user_id="999", ig_access_token="tok", ig_app_secret="secret",
                    encryption_key=generate_key(), database_url=f"sqlite:///{tmp_path}/t.db",
                    autonomy_mode="auto", knowledge_dir=str(ROOT / "knowledge"), max_posts_per_day=3,
                    post_approval=False)


@pytest.fixture
def llm():
    return FakeLLM()


@pytest.fixture
def ig():
    m = MagicMock()
    m.get_media.return_value = {"id": "m1", "caption": "Avaliações físicas na ApexPT"}
    m.publishing_limit.return_value = {"quota_usage": 0, "config": {"quota_total": 100}}
    m.create_container.side_effect = lambda **kw: f"c-{kw.get('media_type')}"
    m.publish.return_value = "ig-media-1"
    return m


@pytest.fixture
def agent(settings, llm, ig):
    sf = make_session_factory(settings.database_url)
    brain = Brain(llm, model_classify="h", model_draft="s", knowledge_dir=settings.knowledge_dir)
    return Agent(settings, sf, ig, brain, Limiter(fakeredis.FakeRedis()), RecordingNotifier())
