import pytest

from guardrail import AgentContext, Guardrail, KnowledgeBase, RiskRouter
from guardrail.app import build_app
from guardrail.config import Settings
from guardrail.reviewers import heuristic_reviewers


@pytest.fixture
def kb():
    return KnowledgeBase.in_memory()


@pytest.fixture
def guard(kb):
    return Guardrail(kb, RiskRouter(*heuristic_reviewers()))


@pytest.fixture
def ada():
    return AgentContext(
        ["Hi, I'm Ada (ada@example.com). Order O-5001 arrived with a cracked lantern. "
         "Can I get a refund for the lantern?"],
        customer_id="C-1001",
    )


@pytest.fixture
def app():
    return build_app(Settings(db_path=":memory:"))

