"""C07 graph source-kind and extractor bytes are bound verbatim."""
import importlib
from common import ConsumerFixture


class GraphCodec(ConsumerFixture):
    def test_graph_fact_bytes_are_verbatim(self):
        try:
            module=importlib.import_module('cortex_core.modules.graph.consumer_adapter')
        except ImportError:
            self.fail('C07 graph fact adapter is missing')
        payload=b'{"source_kind":"knowledge","identity":"extractor-v1","nodes":[],"edges":[]}'
        result=module.bind_graph_fact(payload,source_kind='knowledge',revision=7,extractor='extractor-v1')
        self.assertEqual(result.payload,payload)
        self.assertEqual((result.source_kind,result.revision,result.extractor),('knowledge',7,'extractor-v1'))
