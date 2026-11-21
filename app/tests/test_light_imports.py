"""The dashboard must not import the pipeline's heavy libraries.

They are not installed on Vercel (they are in the "pipeline" dependency group), so importing
one there would crash the dashboard. Leaving them out also keeps the function small.
"""

import subprocess
import sys

HEAVY = ["langgraph", "google.genai", "groq", "trafilatura", "pypdf", "rapidfuzz"]


def test_dashboard_does_not_import_heavy_libraries() -> None:
    # A fresh Python process: this test process has already imported everything.
    code = (
        "import sys, app.main\n"
        f"print(','.join(m for m in {HEAVY!r} if m in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    )
    assert result.stdout.strip() == "", f"app.main imports: {result.stdout.strip()}"
