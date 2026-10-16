import argparse

from src.ui.app import render_app


parser = argparse.ArgumentParser(description="ModeLLM dashboard")
parser.add_argument("--saved-run", help="Open a run directory or result.json without running models")
# Streamlit's test host can also supply its own command-line arguments.
args, _ = parser.parse_known_args()
render_app(saved_run=args.saved_run)
