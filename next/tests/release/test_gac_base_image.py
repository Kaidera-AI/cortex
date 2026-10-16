"""GA-C-R02-001 (Hale P2, kai row c): the sole R02 argv carries the admitted base identity.

The canonical native template gains EXACTLY ONE closed argument,
--build-arg=BASE_IMAGE=sha256:<admitted base config digest>, from the admitted
input's single base_images row for the target platform. The lane refuses before
any root or engine op unless that value equals the base whose Id its input phase
loads. Both committed recipes (Core, API) need exactly what this argv supplies.
Fixture level: a fake engine at the lane's _child seam; no host run here.
"""
from pathlib import Path
import re
import unittest

import test_r4_native_lane as r4

policy, runner = r4.policy, r4.runner
NEXT = Path(__file__).resolve().parents[2]
RECIPES = {m: NEXT / 'release/recipes' / m / 'Containerfile' for m in ('core', 'api')}
AUTOMATIC = {'TARGETARCH', 'TARGETOS', 'TARGETPLATFORM', 'TARGETVARIANT', 'BUILDARCH', 'BUILDOS', 'BUILDPLATFORM'}


def build_args(template):
    return [t.split('=', 1)[1] for t in template if t.startswith('--build-arg=')]


class BaseImageArgument(unittest.TestCase):
    def test_template_carries_exactly_one_admitted_base_argument(self):
        lane = r4.Lane(self)
        template = policy.native_build_template(lane.value)
        self.assertEqual(build_args(template), ['SOURCE_DATE_EPOCH=0', 'BASE_IMAGE=sha256:' + lane.base_config],
                         'BODY: the sole R02 argv must carry the admitted base config digest, once')

    def test_base_row_must_be_unique_and_for_the_target_platform(self):
        lane = r4.Lane(self)
        row = dict(lane.value['dependencies']['base_images'][0])
        for rows in ([], [row, dict(row, config_digest='f' * 64)], [dict(row, platform='linux/arm64')],
                     [dict(row, config_digest='F' * 64)], [dict(row, config_digest='sha256:' + 'a' * 64)]):
            with self.subTest(rows=rows):
                lane.value['dependencies']['base_images'] = rows
                with self.assertRaises(ValueError):
                    policy.native_build_template(lane.value)

    def test_missing_extra_or_changed_base_argument_refuses_before_any_engine_op(self):
        canonical = policy.native_build_template(r4.Lane(self).value)
        token = next(t for t in canonical if t.startswith('--build-arg=BASE_IMAGE='))
        cases = {'missing': [t for t in canonical if t != token],
                 'other_build_arg': canonical[:-1] + ['--build-arg=OTHER=caller', '.'],
                 'second_base': canonical[:-1] + ['--build-arg=BASE_IMAGE=sha256:' + 'f' * 64, '.'],
                 'changed_value': [t if t != token else '--build-arg=BASE_IMAGE=sha256:' + 'f' * 64 for t in canonical],
                 'by_name': [t if t != token else '--build-arg=BASE_IMAGE=docker.io/library/postgres:18' for t in canonical]}
        for name, template in cases.items():
            with self.subTest(case=name):
                lane = r4.Lane(self)
                lane.value['recipe']['argv_template'] = template
                fake = r4.Engine(lane)
                with self.assertRaises(ValueError) as caught:
                    lane.run(fake)
                self.assertEqual(str(caught.exception), 'native_recipe_uncontained')
                self.assertEqual(fake.calls, []); self.assertEqual(lane.roots(), [])

    def test_accepted_base_must_be_the_admitted_base_before_any_engine_op(self):
        lane = r4.Lane(self)
        lane.base = dict(lane.base, config_sha256='f' * 64)  # another base archive than the admitted digest
        fake = r4.Engine(lane)
        with self.assertRaises(ValueError) as caught:
            lane.run(fake)
        self.assertEqual(str(caught.exception), 'native_base_identity')
        self.assertEqual(fake.calls, []); self.assertEqual(lane.roots(), [])

    def test_build_child_passes_the_id_the_input_phase_loaded(self):
        lane = r4.Lane(self)
        record = lane.run(r4.Engine(lane))
        build = record['phases']['build']['children'][0]['argv']
        self.assertIn('--build-arg=BASE_IMAGE=sha256:' + lane.base['config_sha256'], build)
        self.assertEqual(build[8:], lane.value['recipe']['argv_template'])


class BothRecipes(unittest.TestCase):
    """The accepted command builds BOTH committed recipes; nothing else is needed."""

    def test_each_recipe_needs_exactly_what_the_canonical_argv_supplies(self):
        canonical = policy.native_build_template(r4.Lane(self).value)
        supplied = {a.split('=', 1)[0] for a in build_args(canonical)}
        for module, path in RECIPES.items():
            with self.subTest(recipe=module):
                text = path.read_text()
                declared = dict(re.findall(r'^ARG\s+([A-Z_][A-Z0-9_]*)(?:=(\S*))?\s*$', text, re.M))
                required = {name for name, default in declared.items() if not default} - AUTOMATIC
                self.assertEqual(required, {'BASE_IMAGE'}, 'BODY: recipe requires an argument the runner never supplies')
                self.assertLessEqual(required, supplied)
                self.assertRegex(text, r'(?m)^FROM \$\{BASE_IMAGE\}( AS \S+)?$')
                self.assertNotIn('FROM docker.io', text, 'BODY: a recipe pulls a base by name instead of the admitted Id')

    def test_lane_runs_each_real_recipe_as_its_dockerfile_through_the_canonical_runner(self):
        for module, path in RECIPES.items():
            with self.subTest(recipe=module):
                lane = r4.Lane(self)
                raw = path.read_bytes()
                lane.materialize = lambda dest, raw=raw: (Path(dest) / 'Dockerfile').write_bytes(raw)
                fake = r4.Engine(lane)
                record = lane.run(fake)
                child = record['phases']['build']['children'][0]
                self.assertEqual(child['argv'][8:], policy.native_build_template(lane.value))
                self.assertIn('--file=Dockerfile', child['argv'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
