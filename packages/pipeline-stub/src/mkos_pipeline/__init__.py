"""The pipeline stub: `from mkos_pipeline import run_listing_pipeline` (CANON.md:104), since SI7
`edit_image` (the second AI-seam entry point), since SI12 `read_size_chart` (the third) and since
SI13 the one-call JSON helper `complete_json` (the backend's error-diagnosis prompt), and since
SI14 `classify_product` and `map_columns` (the fourth and fifth entry points), since V1.1
`read_photo_facts` (the photo read on upload), and since client feedback item 9 `photo_match`
(does a photo show this product in its colour)."""

from .classify import classify_product
from .columns import map_columns
from .graph import run_listing_pipeline
from .image_edit import edit_image
from .llm import complete_json
from .photo_facts import read_photo_facts
from .photo_match import photo_match
from .size_chart import read_size_chart

__all__ = [
    "classify_product",
    "complete_json",
    "edit_image",
    "map_columns",
    "photo_match",
    "read_photo_facts",
    "read_size_chart",
    "run_listing_pipeline",
]
