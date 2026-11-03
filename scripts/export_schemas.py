"""Export machine-readable provisional contracts; no dataset schema is inferred."""

import json
from pathlib import Path

from core.contracts import FeatureWindow, SignedReading, VerifiedReading


def main() -> None:
    destination = Path("docs/schemas")
    destination.mkdir(parents=True, exist_ok=True)
    for model in (SignedReading, VerifiedReading, FeatureWindow):
        (destination / f"{model.__name__}.json").write_text(
            json.dumps(model.model_json_schema(mode="serialization"), indent=2) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
