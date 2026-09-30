"""Safe shared DEMO preview. Does not import any existing application."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.household.app import Config, create_app


def main():
    import uvicorn
    # Deliberately ignore all production storage and credential environment variables.
    with tempfile.TemporaryDirectory(prefix="household-demo-") as directory:
        path = Path(directory)
        (path / ".household-demo").touch()
        app = create_app(Config(data_dir=path, production=False, preview=True))
        print("Household DEMO preview: temporary fictional data only; inference and MCP credentials disabled.")
        uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("HOUSEHOLD_PORT", os.getenv("PORT", "8099"))),
                    proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()