from mkos_pipeline.prompts import load_prompt, prompt_version

NAMES = [
    "system",
    "content",
    "keyword",
    "attribute",
    "content_correct",
    "keyword_correct",
    "attribute_correct",
]


def test_registered_prompts_load() -> None:
    for name in NAMES:
        text = load_prompt(name, "v1")
        assert text.strip()
        version = prompt_version(name, "v1")
        assert version.startswith(f"{name}/v1+")
        assert len(version.split("+")[1]) == 12
