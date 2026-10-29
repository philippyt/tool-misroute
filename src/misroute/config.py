from pathlib import Path
from typing import Any, Literal
import yaml
from pydantic import BaseModel, Field, model_validator

Provider = Literal["anthropic", "anthropic-foundry", "bedrock", "openai-compatible", "mock"]
Mode = Literal["json", "code"]


class ServerConfig(BaseModel):
    url: str | None = None
    command: list[str] | None = None
    openapi: str | None = None
    auth: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    env: dict[str, str] = Field(default_factory=dict)
    include_tools: list[str] = Field(default_factory=list)
    exclude_tools: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_target(self):
        if sum(map(bool, (self.url, self.command, self.openapi))) != 1:
            raise ValueError("server needs exactly one of url, command or openapi")
        return self


class ModelConfig(BaseModel):
    name: str
    provider: Provider
    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    price_in: float | None = None
    price_out: float | None = None
    concurrency: int = 4
    max_tokens: int = 1024
    scoring: bool = False
    options: dict[str, Any] = Field(default_factory=dict)

    @property
    def model_id(self) -> str:
        return self.model or self.name


class Limits(BaseModel):
    max_wrong_rate: float | None = None
    max_write_hedge_rate: float | None = None
    max_code_failure_rate: float | None = None


class GuardConfig(BaseModel):
    hold_sibling_writes: bool = True
    same_turn_secs: float = 2.0
    switch_secs: float = 30.0
    siblings: list[list[str]] = Field(default_factory=list)
    log: str = ".misroute/guard.jsonl"
    otel: bool = False


class Config(BaseModel):
    server: ServerConfig
    models: list[ModelConfig]
    modes: list[Mode] = ["json", "code"]
    budget_usd: float = 5.0
    generator: str | None = None
    validator: str | None = None
    writer: str | None = None
    requests_per_tool: int = 6
    slots: int = 4
    rotations: int = 3
    repeats: int = 3
    max_turns: int = 1
    real_requests: str | None = None
    workdir: str = ".misroute"
    limits: Limits = Field(default_factory=Limits)
    guard: GuardConfig = Field(default_factory=GuardConfig)

    def model(self, name: str) -> ModelConfig:
        for m in self.models:
            if m.name == name:
                return m
        raise KeyError(f"unknown model {name}")

    def tested(self) -> list[str]:
        """Models under test: every model not configured only as generator, validator or writer."""
        roles = {self.generator, self.validator, self.writer} - {None}
        return [m.name for m in self.models if m.name not in roles] or [m.name for m in self.models]

    def role(self, role: str) -> ModelConfig:
        name = getattr(self, role) or self.models[0].name
        return self.model(name)


def load(path: str | Path) -> Config:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return Config.model_validate(data)
