"""Bounded keyword reads over the existing current authorized Core read port."""
from dataclasses import dataclass
import hashlib

from .projection_revision import CapabilityUnavailable, request_deadline, check_deadline
from .text import project_text, UnsupportedText


@dataclass(frozen=True)
class KeywordHit:
    record_id: str
    kind: str
    source_revision: int
    score: float=1.0


@dataclass(frozen=True)
class UnsupportedRecord:
    record_id: str
    reason: str


@dataclass(frozen=True)
class KeywordResult:
    hits: tuple
    complete: bool
    reason: str | None
    unsupported: tuple
    scanned_records: int
    source_state: tuple


def current_rows(rows, bound):
    if not isinstance(rows,(tuple,list)) or len(rows)>bound:
        raise CapabilityUnavailable('canonical_read_unbound')
    values=[]
    for row in rows:
        rid=getattr(row,'record_id',None);kind=getattr(row,'kind',None)
        revision=getattr(row,'source_revision',None);payload=getattr(row,'payload',None)
        if (not isinstance(rid,str) or not rid or not isinstance(kind,str)
                or type(revision) is not int or not 0<revision<2**63 or type(payload) is not bytes):
            raise CapabilityUnavailable('canonical_read_unbound')
        values.append((rid,kind,revision,payload))
    values.sort(key=lambda row:row[0])
    if len({v[0] for v in values})!=len(values):
        raise CapabilityUnavailable('canonical_read_unbound')
    return tuple(values)


def source_state(rows):
    return tuple((rid,kind,rev,hashlib.sha256(payload).hexdigest()) for rid,kind,rev,payload in rows)


class KeywordSearch:
    def __init__(self, *, read_current, authorize, max_records=100):
        if (not callable(read_current) or not callable(authorize)
                or type(max_records) is not int or not 1<=max_records<=100):
            raise ValueError('keyword read ports/bound are invalid')
        self.read_current,self.authorize,self.max_records=read_current,authorize,max_records

    async def search(self, subject, query, *, limit=20, deadline):
        if (not isinstance(query,str) or not 1<=len(query.strip())<=512
                or type(limit) is not int or not 1<=limit<=100):
            raise ValueError('invalid keyword request')
        deadline=request_deadline(deadline)
        check_deadline(deadline)
        if await self.authorize(subject) is False:
            raise PermissionError('forbidden')
        check_deadline(deadline)
        rows=current_rows(await self.read_current(subject,limit=self.max_records+1,deadline=deadline),self.max_records+1)
        check_deadline(deadline)
        hits=[];unsupported=[];query_fold=query.casefold()
        for rid,kind,revision,payload in rows[:self.max_records]:
            check_deadline(deadline)
            try:text=project_text(kind,payload)
            except UnsupportedText as error:
                unsupported.append(UnsupportedRecord(rid,error.reason))
                continue
            if query_fold in text.casefold():
                hits.append(KeywordHit(rid,kind,revision))
        check_deadline(deadline)
        # Repeat only the same bounded current view. Compare identities, kinds,
        # revisions and payload hashes, including the one overflow witness.
        observed=current_rows(await self.read_current(subject,limit=self.max_records+1,deadline=deadline),self.max_records+1)
        check_deadline(deadline)
        state=source_state(rows)
        if source_state(observed)!=state:
            raise CapabilityUnavailable('source_changed')
        check_deadline(deadline)
        if await self.authorize(subject) is False:
            raise PermissionError('forbidden')
        check_deadline(deadline)
        reason='scan_limit' if len(rows)>self.max_records else ('unsupported_text' if unsupported else None)
        return KeywordResult(tuple(hits[:limit]),reason is None,reason,tuple(unsupported),min(len(rows),self.max_records),state)
