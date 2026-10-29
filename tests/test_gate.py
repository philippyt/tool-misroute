from misroute.requests.gate import gate
from misroute.server.catalog import Tool
from misroute.trial import Request

A = Tool("get_case_comments", "Written comments posted on a case.")
B = Tool("get_case_events", "Timeline events of a case.")
SLOTS = ["1", "2", "3", "4"]


def reqs(tool, texts):
    return [Request(f"{tool}{i}", tool, t, SLOTS) for i, t in enumerate(texts)]


def test_gate_flags_giveaway_requests():
    ra = reqs(A.name, [f"show written comments posted on case {{slot}} {i}" for i in range(6)])
    rb = reqs(B.name, [f"what happened to {{slot}} {i}" for i in range(6)])
    assert gate(A, B, ra, rb) <= 0.10


def test_gate_passes_balanced_requests():
    words = [
        "what did people say about case",
        "anything new on case",
        "who touched case",
        "status of case",
        "notes left on case",
        "history for case",
    ]
    ra = reqs(A.name, [f"{w} {{slot}}" for w in words])
    rb = reqs(B.name, [f"{w} {{slot}}" for w in words[::-1]])
    assert gate(A, B, ra, rb) > 0.10
