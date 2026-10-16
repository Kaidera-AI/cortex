"""Hale R6 round-2 composition counterexamples (hale-r6-r2-code-review-20261016/REVIEW_PROBES.py;
import ported, body verbatim). Four-key R6 re-review: run-bound teardown evidence, using the author's composition.

No product edits or native operations. The existing fake host, engines and smoke
contract are reused. A completed marker is placed in the supplied operator root
before setup, so the entry must refuse reuse or perform and prove its own cleanup.
"""
import unittest
from pathlib import Path
from unittest.mock import patch

import test_r6_native_operator as r6  # ported (nix): module import, so its Composition class is not collected twice
from test_r6_native_operator import Lane, RUN, BASELINE, OURS, codec, runner

WITNESSES = {}


class Boundaries(unittest.TestCase):
    def test_control_empty_operator_root_cleans_the_current_run(self):
        drive, seen, host = r6.Composition.enter(self, Lane(self))
        WITNESSES['empty_root'] = dict(drive=drive, run_result=seen['run']['result'],
                                     teardown=seen['run']['teardown'], packages=host.packages,
                                     user=host.user, host_calls=host.calls)
        self.assertEqual((drive['result'], seen['run']['result']), ('PASS', 'PASS'))
        self.assertEqual(sorted(host.packages), sorted(BASELINE))
        self.assertFalse(host.user)
        r6.Composition.once(self, seen, host)

    def marked_run(self, marker_id, key):
        original = runner.operator_run
        marker = {'run_id': marker_id, 'complete': True, 'remaining_owned_resources': []}

        def with_prior_marker(*args, **kwargs):
            # The product has no fresh-operator-root or marker/run-id admission.
            # Keep the real operator entry and all setup/work/finally execution.
            if marker_id == RUN:
                # A real completed finally before a later attempted setup. The
                # entry must reject this completed run root before new effects.
                self.assertEqual(runner.operator_teardown(RUN, args[1], args[7], clock=kwargs['clock']), marker)
            else:
                path = Path(args[7]) / 'operator-teardown.json'
                path.write_bytes(codec.canonical_bytes(marker))
                path.chmod(0o444)
            return original(*args, **kwargs)

        with patch.object(runner, 'operator_run', with_prior_marker):
            drive, seen, host = r6.Composition.enter(self, Lane(self))
        after_install = host.calls[host.calls.index('install') + 1:] if 'install' in host.calls else []
        result = seen['run']['result'] if seen['run'] is not None else None
        WITNESSES[key] = dict(marker=marker, drive=drive, run_result=result,
                              teardown=seen['run']['teardown'] if seen['run'] is not None else None, packages=host.packages,
                              user=host.user, host_calls=host.calls,
                              outer_calls=seen['calls'], stop_post_calls=seen['stop_post_calls'],
                              calls_after_install=after_install,
                              marker_created_by_product=marker_id == RUN)
        # Both refusing the completed root and proving actual cleanup are valid.
        # AUDIT.py separately checks the current witness and failure attribution.
        clean = not host.user and sorted(host.packages) == sorted(BASELINE)
        refused = drive['result'] != 'PASS' and result != 'PASS'
        self.assertTrue(clean or refused,
                        'prior teardown marker admitted as current cleanup; user/packages remain')

    def test_foreign_run_marker_cannot_prove_current_run_cleanup(self):
        self.marked_run('cortex-r6-amd64-another', 'foreign_marker')

    def test_prior_same_identity_marker_cannot_skip_new_setup_cleanup(self):
        self.marked_run(RUN, 'same_identity_marker')
