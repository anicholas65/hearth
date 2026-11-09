"""Write docs/openapi.json from the live API definition:  python -m app.export_openapi"""
import json
from pathlib import Path

from .api import api

if __name__ == "__main__":
    out = Path(__file__).parent.parent / "docs" / "openapi.json"
    out.write_text(json.dumps(api.openapi(), indent=2, sort_keys=True) + "\n")
    print(f"wrote {out} ({len(api.openapi()['paths'])} paths)")
