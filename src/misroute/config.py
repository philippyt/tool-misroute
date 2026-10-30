import os
from pathlib import Path
from typing import Any, Literal
import yaml
from pydantic import BaseModel, Field, model_validator

Provider = Literal["anthropic", "anthropic-foundry", "bedrock", "openai-compatible", "azure-openai", "mock"]
Mode = Literal["json", "code"]


class ServerConfig(BaseModel):
    url: str | None = None
    command: list[str] | None = None
    openapi: str | None = None
    tools: str | None = None
    auth: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    env: dict[str, str] = Field(default_factory=dict)
    include_tools: list[str] = Field(default_factory=list)
    exclude_tools: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_target(self):
        if sum(map(bool, (self.url, self.command, self.openapi, self.tools))) != 1:
            raise ValueError("server needs exactly one of url, command, openapi or tools")
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
    max_tokens: int | None = None
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
    rephrase_overlap: float = 0.5
    siblings: list[list[str]] = Field(default_factory=list)
    log: str = ".misroute/guard.jsonl"
    otel: bool = False


class Config(BaseModel):
    """Run settings. Models can be written as provider:model shorthand, for example anthropic:claude-opus-5-5."""

    server: ServerConfig
    models: list[ModelConfig]
    modes: list[Mode] = ["json", "code"]
    budget_usd: float = 5.0
    generator: str | None = None
    validator: str | None = None
    writer: str | None = None
    requests_per_tool: int = 6
    slots: int = 4
    rotations: int = 2
    repeats: int = 1
    max_turns: int = 1
    real_requests: str | None = None
    request_source: Literal["description", "spec"] = "description"
    request_languages: list[str] = Field(default_factory=lambda: ["en"])
    domain: str | None = None
    workdir: str = ".misroute"
    system_certs: bool = False
    limits: Limits = Field(default_factory=Limits)
    guard: GuardConfig = Field(default_factory=GuardConfig)

    @model_validator(mode="before")
    @classmethod
    def _shorthand(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        from misroute.models.presets import expand

        data = dict(data)
        if isinstance(data.get("request_languages"), list):
            data["request_languages"] = ["no" if x is False else x for x in data["request_languages"]]
        models = [expand(m) if isinstance(m, str) else m for m in data.get("models") or []]
        names = {m["name"] if isinstance(m, dict) else m.name for m in models}
        for role in ("generator", "validator", "writer"):
            value = data.get(role)
            if isinstance(value, str) and ":" in value and value not in names:
                block = expand(value)
                if block["name"] not in names:
                    models.append(block)
                    names.add(block["name"])
                data[role] = block["name"]
        data["models"] = models
        return data

    @model_validator(mode="after")
    def _roles_exist(self):
        names = [m.name for m in self.models]
        for role in ("generator", "validator", "writer"):
            value = getattr(self, role)
            if value and value not in names:
                raise ValueError(f"{role} {value} is not a configured model, models are {', '.join(names)}")
        return self

    def model(self, name: str) -> ModelConfig:
        for m in self.models:
            if m.name == name:
                return m
        raise KeyError(f"unknown model {name}")

    def tested(self) -> list[str]:
        """Models under test: every model not configured only as generator, validator or writer."""
        roles = {self.generator, self.validator, self.writer} - {None}
        return [m.name for m in self.models if m.name not in roles] or [m.name for m in self.models]

    def role_warnings(self) -> list[str]:
        """Roles played by a tested model or its family, which lets one model write, judge and pass its own test."""
        tested = [self.model(n) for n in self.tested()]
        out = []
        for role in ("generator", "validator", "writer"):
            rm = self.role(role)
            for t in tested:
                if rm.name == t.name:
                    out.append(f"{role} is the model under test ({t.name}), set {role} to a model from another family")
                elif family(rm) == family(t):
                    out.append(f"{role} {rm.name} is the same family as the tested {t.name}, prefer another family")
        return list(dict.fromkeys(out))

    def role(self, role: str) -> ModelConfig:
        name = getattr(self, role) or self.models[0].name
        return self.model(name)


FAMILIES = (
    ("claude", "anthropic"),
    ("gpt", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("o4", "openai"),
    ("gemini", "google"),
    ("gemma", "google"),
    ("deepseek", "deepseek"),
    ("qwen", "qwen"),
    ("llama", "meta"),
    ("mistral", "mistral"),
    ("mixtral", "mistral"),
    ("grok", "xai"),
    ("phi", "microsoft"),
    ("command", "cohere"),
)


def family(m: ModelConfig) -> str:
    if m.options.get("family"):
        return str(m.options["family"])
    model = m.model_id.lower()
    return next((fam for key, fam in FAMILIES if key in model), m.provider)


def env_path(config: str | Path) -> Path:
    return Path(config).resolve().parent / ".env"


def write_env(config: str | Path, secrets: dict[str, str]) -> None:
    """Add or replace KEY=VALUE lines in .env next to the config and in the current environment."""
    if not secrets:
        return
    path = env_path(config)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = [ln for ln in lines if ln.split("=", 1)[0].strip() not in secrets]
    path.write_text("\n".join([*lines, *(f"{k}={v}" for k, v in secrets.items())]) + "\n", encoding="utf-8")
    os.environ.update(secrets)


def load_env(config: str | Path) -> None:
    """Read KEY=VALUE lines from .env next to the config into the environment, without overriding set values."""
    path = env_path(config)
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() and not key.lstrip().startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load(path: str | Path) -> Config:
    load_env(path)
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return Config.model_validate(data)
