"""Export the actual FastAPI contract without starting external services."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".python_packages"))

from agent_system.api.routes import app

if __name__ == "__main__":
    target = ROOT / "docs" / "openapi.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Exported {target}")
