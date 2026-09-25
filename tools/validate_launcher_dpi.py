"""Print the launcher 100%-200% and cross-monitor acceptance matrix."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cuoti_app.services.dpi_validation import validate_matrix


if __name__ == "__main__":
    print(json.dumps(validate_matrix(), ensure_ascii=False, indent=2))
