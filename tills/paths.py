"""Shared path constants for all pipeline scripts."""
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT       = SCRIPT_DIR.parent              # supersplat/
DATA       = ROOT / "CameraData"

def project(name: str) -> Path:
    """Return path to a project directory under CameraData/."""
    return DATA / name
