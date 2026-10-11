import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
LANGUAGES={'c','cpp','csharp','dart','go','java','javascript','kotlin','php','python','ruby','rust','solidity','swift','tsx','typescript'}
def test_complete_pinned_platform_coverage():
 pins=json.loads((ROOT/'tree-sitter-grammar-pins.json').read_text());assert set(pins['supported_languages'])==LANGUAGES
 assert set(pins['platforms'])=={'linux-x86_64','linux-aarch64'}
 for row in pins['platforms'].values():assert len(row['sha256'])==64 and row['size']>0
def test_immutable_cache_survives_final_image_and_cleanup():
 source=(ROOT/'Dockerfile').read_text();assert 'COPY --from=bcrg-builder /opt/kaidera-grammars /opt/kaidera-grammars' in source;assert 'verify-tree-sitter-grammars.py' in source;assert 'TREE_SITTER_LANGUAGE_PACK_CACHE_DIR=/opt/kaidera-grammars/cache' in source
def test_runtime_configures_verified_offline_parsers():
 source=(ROOT/'worker.py').read_text();assert 'verify-tree-sitter-grammars.py' in source;assert 'graph_environment_error' in source
