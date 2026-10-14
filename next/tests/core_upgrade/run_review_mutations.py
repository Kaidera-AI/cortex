"""Current-head repair/operation controls use the same strict host mutator."""
from run_host_mutations import main

if __name__=='__main__':
    raise SystemExit(main(['pinned_key','backup_fence','rollback_marker','journal_cas',
                          'advance_lock','accept_lock','preservation']))
