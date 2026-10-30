import re
from dataclasses import dataclass
from misroute.config import GuardConfig


@dataclass
class Event:
    kind: str
    tool: str
    other: str | None = None
    detail: str = ""


def jaccard(a: str, b: str) -> float:
    x, y = set(re.findall(r"\w+", a.lower())), set(re.findall(r"\w+", b.lower()))
    return len(x & y) / len(x | y) if x | y else 0.0


class Rules:
    def __init__(self, siblings: list[list[str]], writes: set[str], cfg: GuardConfig):
        self.cfg = cfg
        self.writes = writes
        self.siblings: dict[str, set[str]] = {}
        for pair in siblings:
            for a in pair:
                self.siblings.setdefault(a, set()).update(x for x in pair if x != a)

    def check(self, name: str, history: list[tuple[str, float]], now: float) -> list[Event]:
        out = []
        sib = self.siblings.get(name, set())
        for prev, at in reversed(history):
            age = now - at
            if age > self.cfg.switch_secs:
                break
            if prev not in sib:
                continue
            if age <= self.cfg.same_turn_secs:
                out.append(Event("hedge", name, prev))
            else:
                out.append(Event("switch", name, prev))
            if self.cfg.hold_sibling_writes and name in self.writes and prev in self.writes:
                out.append(Event("hold", name, prev, "second write to a sibling tool"))
            break
        return out

    def rephrase(self, text: str, last: str | None, age: float) -> Event | None:
        same = jaccard(text, last) >= self.cfg.rephrase_overlap if last else False
        if last and age <= self.cfg.switch_secs and same and text.strip() != last.strip():
            return Event("rephrase", "", None, text[:200])
        return None
