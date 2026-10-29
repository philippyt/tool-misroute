from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Request:
    id: str
    tool: str
    template: str
    slots: list[str]
    kind: str = "goal"
    sibling: str | None = None

    def texts(self) -> list[tuple[str, str]]:
        if "{slot}" not in self.template or not self.slots:
            return [("", self.template)]
        return [(s, self.template.replace("{slot}", s)) for s in self.slots]


@dataclass
class Trial:
    model: str
    mode: str
    target: str
    request_id: str
    text: str
    kind: str = "goal"
    slot: str = ""
    order: int = 0
    rep: int = 0
    phase: str = ""
    condition: str = "original"
    pair: str = ""
    calls: list[str] = field(default_factory=list)
    failure: str | None = None
    probs: dict[str, float] | None = None
    detail: str = ""

    @property
    def chosen(self) -> str | None:
        return self.calls[0] if self.calls else None

    @property
    def hedge(self) -> bool:
        return len(set(self.calls)) > 1

    @property
    def wrong(self) -> bool:
        return self.chosen is not None and self.chosen != self.target

    @property
    def error(self) -> bool:
        return self.chosen != self.target or self.hedge

    @property
    def key(self) -> tuple:
        return (self.model, self.mode, self.request_id, self.slot, self.order, self.rep)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)
