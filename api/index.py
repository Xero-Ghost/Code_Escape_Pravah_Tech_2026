"""Single WSGI entry point for all /api/* routes on Vercel."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app import app
