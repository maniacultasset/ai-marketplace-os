from pydantic import BaseModel, ConfigDict, Field


class ClaimDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    fact_keys: list[str] = Field(default_factory=list)


class ContentDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    bullets: list[str]
    description: str
    claims: list[ClaimDraft] = Field(default_factory=list)


class KeywordDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    search_terms: list[str]
    hidden_terms: list[str] = Field(default_factory=list)


class AttributePick(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    value: str
    source_fact: str


class AttributeDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attributes: list[AttributePick] = Field(default_factory=list)
