"""Bounded history provenance plan. No DB writes or parser execution."""
import json,os,re,sys,uuid
from pathlib import Path


def inputs(claude,codex):
    result=[]
    for provider,file in [('claude',claude),('codex',codex)]:
        for raw in Path(file).read_bytes().decode().split('\0'):
            if not raw:continue
            p=Path(raw).resolve()
            if provider=='claude':
                try:id=str(uuid.UUID(p.stem))
                except ValueError:id=str(uuid.uuid5(uuid.NAMESPACE_URL,str(p)))
            else:
                found=re.findall(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',p.stem)
                if not found:raise ValueError('unidentified-codex-input')
                id=str(uuid.UUID(found[-1]))
            result.append((id,provider,p))
    return result


def plan(rows,candidates,script_dir,project):
    by_id={r['id']:r for r in rows}; entries=[];seen=set();fatal=False
    for id,provider,p in candidates:
        old=by_id.get(id);reason='';action='REPLACE'
        parser=Path(script_dir)/('cortex-ingest-session' if provider=='claude' else 'cortex-ingest-codex')
        if id in seen:reason='duplicate-input';action='REFUSED'
        elif any(ord(c)<32 or c=='|' for c in str(p)):reason='unsupported-path';action='REFUSED'
        elif old and old['project']!=project:reason='cross-project';action='REFUSED'
        elif not p.is_file() or not os.access(p,os.R_OK):reason='unreadable-source';action='PRESERVE'
        elif not parser.is_file() or not os.access(parser,os.X_OK):reason='parser-unavailable';action='PRESERVE';fatal=fatal or provider=='claude'
        elif old and (old.get('note_source') is not None or old.get('provider')!=provider
              or old.get('source_kind')!=provider+'-session' or not old.get('source_path')
              or Path(old['source_path']).resolve()!=p
              or any(x not in {'local-file',None} for x in old.get('message_sources',[]))):
            reason='untrusted-or-untagged-provenance';action='REFUSED'
        entries.append({'action':action,'id':id,'provider':provider,'path':str(p),'reason':reason});seen.add(id)
    for id,old in sorted(by_id.items()):
        if id in seen or old['project']!=project:continue
        provider=old.get('provider');path=old.get('source_path');reason='non-transcript-or-untagged'
        if provider=='codex':
            reason='parser-unavailable'
            if not path or not Path(path).is_file() or not os.access(path,os.R_OK):
                reason+='; ORPHAN missing-or-unreadable-source'
        elif provider=='claude' and path and (not Path(path).is_file() or not os.access(path,os.R_OK)):
            reason='ORPHAN missing-or-unreadable-source'
        elif provider=='claude':reason='source-not-discovered'
        entries.append({'action':'PRESERVE','id':id,'provider':provider or 'unknown','path':'','reason':reason})
    return {'fatal':fatal,'entries':entries}


if __name__=='__main__':
    try:
        mode=sys.argv[1]
        if mode=='ids':
            ids=sorted({id for id,_,_ in inputs(sys.argv[2],sys.argv[3])})
            print(','.join("'"+id+"'" for id in ids) or 'NULL')
        elif mode=='plan':
            rows=json.loads(Path(sys.argv[2]).read_text());assert isinstance(rows,list)
            print(json.dumps(plan(rows,inputs(sys.argv[3],sys.argv[4]),sys.argv[5],sys.argv[6])))
        elif mode=='report':
            d=json.loads(Path(sys.argv[2]).read_text())
            for e in d['entries']:print(e['action']+' '+e['id']+' '+e['provider']+' '+e['reason'])
        elif mode=='execute-list':
            d=json.loads(Path(sys.argv[2]).read_text())
            if d['fatal']:sys.exit(2)
            for e in d['entries']:
                if e['action']=='REPLACE':print('|'.join([e['id'],e['provider'],e['path']]))
        else:raise ValueError('invalid-mode')
    except Exception:
        print('ERROR: history provenance plan unavailable; no history was purged.',file=sys.stderr);sys.exit(2)
