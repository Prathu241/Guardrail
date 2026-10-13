import pytest

from guardrail import AgentContext, Guardrail, KnowledgeBase, RiskRouter
from guardrail.reviewers import heuristic_reviewers


@pytest.fixture
def kb():
    return KnowledgeBase.load()


@pytest.fixture
def guard(kb):
    return Guardrail(kb, router=RiskRouter(*heuristic_reviewers()))


@pytest.fixture
def ada():
    return AgentContext(
        ["Hi, I'm Ada (ada@example.com). Order O-5001 arrived with a cracked lantern. "
         "Can I get a refund for the lantern?"],
        customer_id="C-1001",
    )
