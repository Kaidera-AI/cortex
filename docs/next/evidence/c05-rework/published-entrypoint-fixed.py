"""Published C05 entry point; current source proof lives beside its pinned helper."""
from pathlib import Path
import runpy
runpy.run_path(str(Path(__file__).resolve().parent.parent/'c05-rework/run-core-pg.py'),run_name='__main__')
