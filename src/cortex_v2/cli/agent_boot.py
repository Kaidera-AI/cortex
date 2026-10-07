"""Own-member boot read with physical, exact revision body validation."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import uuid
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode, urlsplit

from ..clients.config import _validate_base_url
from ..clients.errors import ClientConfigError
from ..clients.key_store import KeyStoreError
from ..clients.transport import http_request

USAGE = 'Usage: cortex-boot <agent> [--budget N] [--query <text>] [--full]\n\nPrints boot context for an agent in the current Cortex project.\n'
LABEL = r'[a-z][a-z0-9_-]{0,127}'
MAX_BODY = 8 * 1024 * 1024


class BootRefused(ClientConfigError):
    """Only controlled, non-secret diagnostics cross this boundary."""


def require(condition):
    if not condition:
        raise BootRefused('boot response contract refused')


def prepare(args):
    value = args.agent
    require(isinstance(value, str) and len(value) <= 400)
    match = re.fullmatch('('+LABEL+')(?:@('+LABEL+'))?(?::[A-Za-z][A-Za-z0-9_-]{0,127})?', value.lower())
    require(match is not None)
    budget = getattr(args, 'budget', None) or '1200'
    require(isinstance(budget, str) and re.fullmatch(r'[1-9][0-9]{0,6}', budget) is not None)
    query = getattr(args, 'query', None)
    require(query is None or isinstance(query, str) and len(query) <= 4096 and '\x00' not in query)
    pairs = [('budget', budget)]
    if getattr(args, 'full', False): pairs.append(('full', 'true'))
    if query is not None: pairs.append(('query', query))
    return match[1], match[2], '/boot/'+match[1]+'?'+urlencode(pairs)


def prepare_raw(path, agent_name):
    require(isinstance(path,str) and len(path) <= 16384)
    parsed = urlsplit(path)
    require(not parsed.scheme and not parsed.netloc and not parsed.fragment)
    match = re.fullmatch('/boot/('+LABEL+')', parsed.path)
    require(match is not None and agent_name == match[1])
    pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    require(len({k for k,v in pairs}) == len(pairs) and all(k in ('budget','full','query') for k,v in pairs))
    values = dict(pairs)
    require(values.get('full','true') == 'true')
    args=SimpleNamespace(agent=match[1],budget=values.get('budget'),query=values.get('query'),full='full' in values)
    agent, project, unused = prepare(args)
    return agent, project, path


def _uuid(value):
    require(isinstance(value,str) and str(uuid.UUID(value)) == value)


def _revision(value):
    require(type(value) is int and value > 0)


def _relative(value):
    require(isinstance(value,str) and value and not value.startswith('/') and '\\' not in value and '\x00' not in value)
    parts=value.split('/')
    require(all(p not in ('','.','..') for p in parts))
    return parts


def _root(value):
    require(isinstance(value,str) and value.startswith('/') and '\x00' not in value)
    parts=value.split('/')[1:]
    require(parts and all(p not in ('','.','..') for p in parts))
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in parts:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
        require(os.fstat(fd).st_uid == os.geteuid())
        return fd
    except BaseException:
        os.close(fd);raise


def _digest_file(root_fd,ref):
    parts=_relative(ref);fd=os.dup(root_fd)
    try:
        for part in parts[:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
        body_fd=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        with os.fdopen(body_fd,'rb') as body:
            before=os.fstat(body.fileno())
            require(stat.S_ISREG(before.st_mode) and before.st_uid==os.geteuid() and before.st_nlink==1 and before.st_size<=MAX_BODY)
            raw=body.read(MAX_BODY+1);after=os.fstat(body.fileno())
            require(len(raw)<=MAX_BODY and (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)==(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns))
            return hashlib.sha256(raw).hexdigest()
    finally:
        os.close(fd)


def _pairs(items):
    value={}
    for key,item in items:
        require(key not in value);value[key]=item
    return value


def validate(raw,profile,agent):
    require(len(raw)<=32*1024*1024)
    def nonfinite(value): raise BootRefused('boot response contract refused')
    def finite_float(value):
        parsed=float(value)
        require(math.isfinite(parsed))
        return parsed
    data=json.loads(raw,object_pairs_hook=_pairs,parse_constant=nonfinite,parse_float=finite_float)
    require(isinstance(data,dict) and isinstance(data.get('boot'),str) and isinstance(data.get('surface_version'),str))
    persona=data.get('persona');require(isinstance(persona,dict))
    project=profile.default_scope
    require(persona.get('schema_version')=='cortex.persona.v2' and persona.get('agent')==agent and persona.get('project')==project and persona.get('agent_identity')==agent+'@'+project)
    metadata=persona.get('metadata');require(isinstance(metadata,dict))
    proof=metadata.get('boot_validation');require(isinstance(proof,dict))
    require(proof.get('schema_version')=='cortex.boot_validation.v1' and proof.get('project_key')==project)
    _uuid(proof.get('actor_id'));_uuid(proof.get('project_scope_id'));_revision(proof.get('agent_binding_revision'))
    ref=proof.get('persona_ref');require(isinstance(ref,dict) and ref.get('scope_id')==proof['project_scope_id'])
    _uuid(ref.get('persona_id'));_revision(ref.get('revision'))
    root=getattr(profile,'workspace_root',None)
    require(root is not None and root==proof.get('workspace_root'))
    pins=proof.get('body_pins');require(isinstance(pins,list))
    selected={}
    for kind in ('skill','rule'):
        rows=persona.get(kind+'s');require(isinstance(rows,list))
        for row in rows:
            require(isinstance(row,dict))
            slug=row.get(kind+'_slug');require(isinstance(slug,str) and re.fullmatch(LABEL,slug) is not None)
            key=kind,slug;require(key not in selected);selected[key]=row
    require(len(pins)==len(selected))
    root_fd=_root(root)
    try:
        seen=set()
        for pin in pins:
            require(isinstance(pin,dict) and set(pin)=={'entry_kind','slug','scope_id','entry_id','revision','body_ref','body_sha256','binding_ref','publication_ref'})
            key=pin.get('entry_kind'),pin.get('slug')
            require(key in selected and key not in seen);seen.add(key)
            kind,slug=key
            try:
                _uuid(pin.get('scope_id'));_uuid(pin.get('entry_id'));_revision(pin.get('revision'))
                digest=pin.get('body_sha256');require(isinstance(digest,str) and re.fullmatch('[0-9a-f]{64}',digest) is not None)
                binding=pin.get('binding_ref');publication=pin.get('publication_ref')
                require(binding is not None or publication is not None)
                for record,id_name in ((binding,'binding_id'),(publication,'publication_id')):
                    if record is not None:
                        require(isinstance(record,dict));_uuid(record.get(id_name));_revision(record.get('revision'))
                require(publication is not None or pin['scope_id']==proof['project_scope_id'])
                row=selected[key];body_ref=pin.get('body_ref')
                if kind=='skill':require(body_ref is not None and row.get('body_ref')==body_ref)
                if kind=='rule':
                    require(isinstance(row.get('body'),str) and hashlib.sha256(row['body'].encode()).hexdigest()==digest)
                if body_ref is None:
                    require(kind=='rule' and isinstance(row.get('body'),str))
                    actual=hashlib.sha256(row['body'].encode()).hexdigest()
                else:actual=_digest_file(root_fd,body_ref)
                require(actual==digest)
            except (OSError,ValueError,TypeError,BootRefused):
                raise BootRefused('boot body refused: '+slug) from None
    finally:
        os.close(root_fd)


def run(profile,args,prepared=None):
    agent,project,path=prepared or prepare(args)
    reader=profile.member_reader
    require(reader is not None and profile.default_scope==reader.project and (project is None or project==reader.project) and profile.principal_label==agent and all(s==reader.project for s in profile.default_read_scopes))
    try:headers=reader.headers()
    except KeyStoreError:raise BootRefused('member credential unavailable; ask the project lead for enrollment or unlock') from None
    headers=dict(headers);headers['X-Agent-Name']=agent
    response=http_request('GET',_validate_base_url(profile.base_url)+path,headers=headers)
    if response.status!=200:raise BootRefused('boot API refused: '+str(response.status))
    try:validate(response.body,profile,agent)
    except BootRefused:raise
    except (OSError,ValueError,TypeError,KeyError,UnicodeError,RecursionError):raise BootRefused('boot response contract refused') from None
    return response.body.decode('utf-8')
