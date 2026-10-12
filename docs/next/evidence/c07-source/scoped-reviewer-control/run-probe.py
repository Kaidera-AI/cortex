"""Declared author entry: exact sealed reviewer probe, only candidate head re-pin."""
import hashlib,subprocess,sys
from pathlib import Path
root=Path(sys.argv[1]).resolve();here=Path(__file__).resolve().parent
probe=here/'scoped-head-probe.py';body=probe.read_bytes()
assert hashlib.sha256(body).hexdigest()=="2710300f59a444b15e2605f940ba7e080fab0a769200057a237613367922261c"
head=subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True).strip()
text=body.decode();old='9f5cbd3b3ede6e074f9619da44c95d88747e61a7'
assert text.count(old)==1
sys.argv=[str(probe),str(root),str(here/'scoped-head-result.json')]
exec(compile(text.replace(old,head),str(probe),'exec'),{'__name__':'__main__'})
