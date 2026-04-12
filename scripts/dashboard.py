"""Streamlit dashboard launcher.

Usage:
    python -m scripts.dashboard
    # or directly:
    streamlit run src/dashboard/app.py
"""

import subprocess
import sys
from pathlib import Path


def main():
    app_path = Path(__file__).resolve().parents[1] / "src" / "dashboard" / "app.py"
    subprocess.run([sys.executable, "-m", "streamlit", "run", str(app_path)])


if __name__ == "__main__":
    main()
