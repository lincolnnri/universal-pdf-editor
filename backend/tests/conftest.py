import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("UPE_WORKERS", "0")
os.environ.setdefault("UPE_WORK_DIR", tempfile.mkdtemp(prefix="upe-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
