"""D2 and D3 joins; metadata and fixture positives are not installed services."""
import asyncio
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID

from cortex_core.api.c11a import load_routes
from cortex_core.runtime import factory


class FactoryJoins(unittest.IsolatedAsyncioTestCase):
    def surface(self, name):
        self.assertTrue(hasattr(factory, name), 'X5_JOIN_SURFACE_ABSENT: '+name)
        return getattr(factory, name)

    async def test_complete_route_inventory_is_immutable_exact_contract_metadata(self):
        inventory = self.surface('route_inventory')()
        self.assertEqual(len(inventory), 88)
        self.assertEqual([dict(row.metadata) for row in inventory], load_routes())
        self.assertTrue(all(row.authority_status == 'UNBOUND' for row in inventory))
        self.assertEqual({row.route_id for row in inventory if row.openkai_production},
                         {row['id'] for row in load_routes() if row['openkai_production']})
        with self.assertRaises(AttributeError): inventory[0].route_id = 'forged'

    async def test_enrollment_missing_x12_refuses_without_readiness_or_scope_calls(self):
        def forbidden(*args): self.fail('missing X12 invoked SQL port')
        reader = self.surface('_enrollment_reader')
        with patch.dict(sys.modules, {'cortex_core.runtime.enrollment': None}):
            port = reader(forbidden, forbidden)
            self.assertEqual(await port(UUID(int=102)), 'unavailable')

    async def test_enrollment_uses_x12_existing_reader_only(self):
        seen = []
        async def state(installation): seen.append(installation); return 'enrollment_required'
        def existing(readiness, scope):
            seen.extend([readiness, scope]); return state
        readiness, scope = object(), object()
        reader = self.surface('_enrollment_reader')
        with patch.dict(sys.modules, {'cortex_core.runtime.enrollment': SimpleNamespace(enrollment_state_reader=existing)}):
            port = reader(readiness, scope)
            self.assertEqual(await port(UUID(int=102)), 'enrollment_required')
        self.assertEqual(seen, [readiness, scope, UUID(int=102)])

    async def test_health_supervisor_down_cannot_publish_ready(self):
        health = self.surface('_installed_health')
        async def core(): return {'core_available': True, 'state': 'supervisor_down'}
        async def enrolled(_): self.fail('supervisor-down must precede enrollment')
        result = await health(core, enrolled, UUID(int=102), deadline=asyncio.get_running_loop().time()+1)
        self.assertEqual(result['readiness'], 'unavailable')

    async def test_health_one_original_deadline_across_sequential_waits(self):
        health = self.surface('_installed_health')
        async def core():
            await asyncio.sleep(.04)
            return {'core_available': True, 'state': 'healthy'}
        async def enrolled(_): await asyncio.sleep(.04); return 'ready'
        loop = asyncio.get_running_loop(); start = loop.time()
        result = await health(core, enrolled, UUID(int=102), deadline=start+.06)
        self.assertEqual(result['readiness'], 'unavailable')
        self.assertLess(loop.time()-start, .15)

    async def test_health_positive_preserves_existing_composer_shape(self):
        health = self.surface('_installed_health')
        async def core(): return {'core_available': True, 'state': 'healthy'}
        async def enrolled(_): return 'enrollment_required'
        self.assertEqual(await health(core, enrolled, UUID(int=102),
            deadline=asyncio.get_running_loop().time()+1),
            {'component':'cortex','status':'ok','readiness':'enrollment_required'})

    async def test_health_cancellation_swallow_cannot_extend_caller_or_publish(self):
        health = self.surface('_installed_health')
        entered, release = asyncio.Event(), asyncio.Event()
        async def core():
            entered.set()
            while not release.is_set():
                try: await release.wait()
                except asyncio.CancelledError: pass
            return {'core_available': True, 'state': 'healthy'}
        async def enrolled(_): self.fail('late health dispatched enrollment')
        loop = asyncio.get_running_loop(); start = loop.time()
        result = await health(core, enrolled, UUID(int=102), deadline=start+.03)
        self.assertTrue(entered.is_set())
        self.assertEqual(result['readiness'], 'unavailable')
        self.assertLess(loop.time()-start, .15)
        self.assertTrue(factory._health_work)
        release.set(); await asyncio.sleep(.02)
        self.assertFalse(factory._health_work)

