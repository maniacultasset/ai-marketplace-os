from pydantic import BaseModel, ConfigDict


class WireModel(BaseModel):
    """Seam types ignore unknown keys so a newer backend can add fields."""

    model_config = ConfigDict(extra="ignore")
