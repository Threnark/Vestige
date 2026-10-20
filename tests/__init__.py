"""Unit tests. Run from the project folder with:  python -m unittest -v

They use only the standard library, need no network and no API key, and never
touch your real mail: AI calls are replaced by a fake provider, and the
OpenAI-compatible adapter is tested against a tiny fake server on localhost."""
import os
import sys

# Make the project's modules importable when tests are run from anywhere.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
