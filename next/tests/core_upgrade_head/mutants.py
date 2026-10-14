"""Run the current one-locus host source controls; no native effects."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'core_upgrade'))
from run_host_mutations import main

if __name__=='__main__':
    raise SystemExit(main(['through', 'prefix', 'checksum', 'unknown_target', 'reentry']))
