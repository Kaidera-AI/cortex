"""Stdlib writer-site inventory. No runtime imports, credentials, or database calls."""
import ast
import hashlib
import json
from pathlib import Path
import re
import sys

DML = re.compile(r'\b(INSERT\s+INTO|UPDATE(?!\s+SET\b)|DELETE\s+FROM)\b',re.I)
IDENTIFIER = r'(?:"(?:""|[^"])+"|[a-z_][a-z0-9_$]*)'
TARGET = re.compile(r'\s*('+IDENTIFIER+r'(?:\s*\.\s*'+IDENTIFIER+r')?)',re.I)
FUNCTION = re.compile(r'CREATE(?:\s+OR\s+REPLACE)?\s+FUNCTION\s+([a-z_][a-z0-9_.]*).*?\bAS\s+\$\$(.*?)\$\$;',re.I|re.S)

# Source-reviewed, normalized ASTs of the complete enclosing functions. A change
# anywhere in one of these functions invalidates its indirect-SQL exception.
KNOWN_INPUT_FUNCTION_SHA256 = {
    ('src/cortex_core/runtime/catalog.py', '_rows'): 'b17120d3777bf74fa5cf85d1b88bde54df77aa8e6220ee9f7c79e2367fe114bc',
    ('src/cortex_core/runtime/catalog.py', 'observe'): '35db9903898abb1e5b7ee95d141b537b1a20861ded8b3778ed24a76cdab7dce1',
    ('src/cortex_core/coordination.py', 'Jobs._load'): 'e10eddc8f089d9ccc97e60e576dedbfe3d39541e3eb8bee0414db28b29407ade',
    ('src/cortex_core/auth.py', 'authorized'): '479d115551dc48de8bbc2e9fba943d21879bf0497f05a5d65610a97dc282ae46',
    ('src/cortex_core/identity.py', 'Identity._control'): '320f495f9fe9144201451e8d59117ac785efe629d24a518fc7365b685da100ba',
    ('src/cortex_core/identity.py', 'Identity._register'): 'f837fa6fdcc01e32d12b74eb9b8a1390a3305bd8ef256fdf28b6b3b460e820b8',
    ('src/cortex_core/records.py', '_private'): '7b69423595de4126e83a8b345bf785b84f62510d05f451eac13981455d8f2e66',
    ('src/cortex_core/migrations.py', 'apply_migrations'): '52072eecb96a95154f4a003f66157ec962a92f76fa923c84abe9b8ebdd77c76d',
    ('src/cortex_core/backup_validation.py', 'read_restored_snapshot'): '138962613394d459cf5c93a5843b2d25a94afc8b8ce2da3b391fea2277ba00eb',
    ('src/cortex_core/release/_pyc_check.py', '_compiled_fields_equal'): 'fad0c5ff2c9164641b78522302295d9a76ea58924a1bb460879e8e131f1a09ce',
    ('src/cortex_core/release/build/audit.py', '_code_equal'): '25fa770e8b9d590251468d0610c4692a940b05cada23e7cc4736b29b84b63d7c',
    ('src/vector_baseline/product_path.py', 'snapshot_corpus'): 'a89b8ce6091ec7c232f6a81f2202d6c04f903f01132d2ddfd6329363ba393f24',
    ('src/cortex_core/migrations.py', 'prepare_migrations'): '068d58f96eab206b695d4154121d6036d7a285f36e0369b78bf96c6827ba9938',
    ('src/cortex_core/release/codec.py', '_validator_class'): 'f4a06714c0db3b1a24356c3c9579bbfef559552b6254d0fb99de646da65f3cd1',
    ('src/cortex_core/release/codec.py', 'schema_validate'): 'a90f530072af970c807dbea7fc7c0a82ddfdb4bed9362af32ab147c6cef86a14',
    ('src/cortex_core/schema/migrate.py', 'apply_forward_migrations'): 'b845ab9b1fedd1786937d3060392ba053046b745583cc56f8ba39f90a70336b0',
    ('src/cortex_core/schema/tests/test_readiness.py', 'ReadinessTests.test_existing_runner_is_only_migration_engine'): '5897b37afa56870f264c902b334e9ab94f5d6147f0e2099a11352160429478b8',
}

# The function pins alone cannot see an aliased caller elsewhere in the module.
# Every source change in a module that owns an exception requires a reviewed pin.
KNOWN_INPUT_FILE_SHA256 = {
    'src/cortex_core/runtime/catalog.py': '7037ac84ecfa02a0e3655c9026336da874b37998fae9e88c8bf0c93e9df8c78f',
    'src/cortex_core/coordination.py': '56c709da4e5caf8d5baceba92d08d60239d1aa07c85d61c1568bb74320d4a70b',
    'src/cortex_core/auth.py': 'cab6320af61cd8beff4923524460413ff3c48bc0ced61f9315919a7fb96567f0',
    'src/cortex_core/identity.py': 'aa5b4139e15a46d1eb161d84d23a3e9a2553e5126b110549d5bf839004a4ea1d',
    'src/cortex_core/records.py': 'e52d86a0cddfa7b98315f87c9904aeb64665e8dc68e353b8280a276ca97a32d3',
    'src/cortex_core/migrations.py': 'a54f8564f61ba6a94e9aed91f85416eec71bac43864415bfa71538230f7d8a35',
    'src/cortex_core/backup_validation.py': '5d3731e58e5217ba4b2fc6f538679fc4bcc85327924c813f67f6f5bc5819a84a',
    'src/cortex_core/release/_pyc_check.py': '1f35d123db1346056e7b932d8437771c1a4f4d3edc87c25b3bcab00aa40508cf',
    'src/cortex_core/release/build/audit.py': '6639168da2ebb9ab1128b50e6fecf72d7f8f4d6250ced97bfe497900ef9ae306',
    'src/vector_baseline/product_path.py': '68776fc8ec72034bcc1dad696ffff8158b178a8291da14c2a7fa5733de41dca1',
    'src/cortex_core/release/codec.py': '4b72872a8f812701f447dc05bb29d70638cfd1eee5ae3ab5d45df256688ee024',
    'src/cortex_core/schema/migrate.py': '7e05599c53272c0015b919e8e93d1c246477fc7ce297db23ebed9cb5ad18c008',
    'src/cortex_core/schema/tests/test_readiness.py': 'b9793ee18dc425e7a5aa4f174c61e4b046df2ebb51ad2f8f58420e811a5af2d7',
}
EXEMPT_SYMBOL_FILES = {
    '_private': {'src/cortex_core/records.py'},
    '_ROLE_QUERY': {'src/cortex_core/auth.py','src/cortex_core/identity.py'},
    '_register': {'src/cortex_core/identity.py'},
    'apply_migrations': {'src/cortex_core/migrations.py'},
    '_load': {'src/cortex_core/coordination.py'},
}


# Closed classifications for the composed PG-B/P2/X3 inventory. These are retained
# scanner rows, not skip rules. Whole source bytes pin callers/authority as well
# as the complete site identity. Any source or site drift requires new review.
REVIEWED_WRITER_SOURCE_SHA256 = {
    'src/cortex_core/runtime/resources.py': '750ffdd615a34a74a8bbd6f99e2f6265e9f0e8458ea52c0f01dd9d978e2516c7',
    'src/cortex_core/runtime/tests/test_resources.py': '528aaf90842109074290701ecb0d33c8f443b60e37edbe214039b0ddcbe19a61',
    'src/cortex_core/schema/tests/test_seed.py': 'f517ff830af89fa040bf433bf1ce22ea33fae7272042daf377a96d2aebc2bda5',
    'schema/coordination/008-c09-external-v2.sql': '9147e294732af2aada64c75f0ff794cb2cd409ed754afa316bdc72755bb9db11',
    "src/cortex_core/auth.py": "b045ce44b1b944b6d84bcc87a62f97f110a4a73db7b648f2cfc35d848b0d9049",
    "schema/auth/005-first-identity.sql": "22964015953d66eebcd31cf2a0b10deaef2e00029c1390673afd3cff4ae6d09d",
    "schema/auth/007-runtime-grants.sql": "fdd629c4537b82f103977c002597e0259d5213ff0065cf64d674ff0aa7eff607",
    "src/cortex_core/install/lifecycle.py": "cfb9c7585cca903600988498888f3a0f9826fbfd903ffd80b36a305c71689ec3",
    "src/cortex_core/install/owner.py": "074ad69cbde63aaa3e0fdc83c03a472a29bd10dbd81deca2e1f8ce54aa8c350d",
    "src/cortex_core/install/recovery.py": "f03408b7446258a78facdbc867f4c4be35745e88c8450fcf054cf8d35bbca756",
    "src/cortex_core/runtime/role_grants.py": "88b603698800e6b82a47ded38232f29324cfdbe0fc779dd2e4003038916e97b7",
    "src/cortex_core/runtime/connections.py": "97064447e2f260f563d81858a2d846b6807d0de4bc5a24eb38e6c3933e88daa5",
    "src/cortex_core/runtime/secret_resolver.py": "1c5aaf62507c06908397fb4e50bd431e8f55493782ecff61cf4b0c7d31a41496"
}


REVIEWED_WRITER_CLASSES = {
    ('src/cortex_core/runtime/resources.py', '_FiniteResources.transaction', 'DYNAMIC', '<dynamic_or_unsupported>', 0, '8428eded23c3e24d87e19ea92bf82927af65cdff705e0babb9ff83b826d6c6e7'): 'Day2_fixed_readonly_transaction_level_choice; two_literal_SET_branches; no_canonical_write; native_HELD',
    ('src/cortex_core/schema/tests/test_seed.py', '_SeedConnection.execute', 'INSERT', '<unqualified>:<dynamic_or_unsupported>', 0, '3b71213c1851775909097cd24575b58407cb4d907d6021780f317693a7f76dfd'): 'Day2_seed_driver_test_REGEX_DATA; no_SQL_execution_or_installed_credit',
    ('src/cortex_core/schema/tests/test_seed.py', '_SeedConnection.execute', 'UPDATE', '<unqualified>:<dynamic_or_unsupported>', 0, '2937013f2181810606b2a799b05bda2849f3e369a20982a4138f0e0a55984ce4'): 'Day2_seed_driver_test_denied_keywords_DATA; no_SQL_execution_or_installed_credit',
    ('src/cortex_core/runtime/tests/test_resources.py', 'CatalogTests.test_observer_uses_one_readonly_snapshot_and_bound_identity_parameters', 'UPDATE', '<unqualified>:<dynamic_or_unsupported>', 0, '6cb78ab1c6bdad2815633292caac2d1fc296f0b416b8b64e867f4563d4d6b1ae'): 'Day2_catalog_test_assertion_DATA; fixed_SQL_keyword_negative; no_SQL_execution',
    ('schema/coordination/008-c09-external-v2.sql', 'coordination.c07_record', 'INSERT', 'coordination.vector_applied_heads', 0, '20df52164a58822d5f5fdc62798b15912878bd92e61b0a01229041a1c4877a62'): 'C09_external_v2_private_receipt_bookkeeping; SOURCE_ADMITTED_21; Core_durability_requires_native_receipts; capability_UNAVAILABLE',
    ('schema/coordination/008-c09-external-v2.sql', 'coordination.c09_bind', 'INSERT', 'coordination.vector_bindings', 0, '3229a056b109ec96b75c4715a2847cb982711c8571ccf4f73e7ee4422bf135d2'): 'C09_external_v2_private_receipt_bookkeeping; SOURCE_ADMITTED_21; Core_durability_requires_native_receipts; capability_UNAVAILABLE',
    ('src/cortex_core/auth.py', 'authorized', 'DYNAMIC', '<dynamic_or_unsupported>', 0, '73b404c4edb916b5bf9f29735fa35621b218f0508f2df3604f10cefabf7c73eb'): 'fixed_C04_role_catalogue_read; SELECT_only; owned_request_SQL_transaction; local_request_ROLE; no_canonical_write_authority; native_HELD',
    ('schema/auth/007-runtime-grants.sql', 'auth.first_identity', 'INSERT', 'auth.credentials', 0, '566abcb49c2a858ed0ae776b31aed25ea8ffefa7b9cc209168132adfbc6b50b3'): 'held_PG_B_installer_enrollment; fixed_login_OID/map; caller_SQL_transaction; C06_identity_fact',
    ('schema/auth/007-runtime-grants.sql', 'auth.first_identity', 'INSERT', 'auth.first_identity_receipts', 0, '566abcb49c2a858ed0ae776b31aed25ea8ffefa7b9cc209168132adfbc6b50b3'): 'held_PG_B_installer_enrollment; fixed_login_OID/map; caller_SQL_transaction; C06_identity_fact',
    ('schema/auth/007-runtime-grants.sql', 'auth.first_identity', 'INSERT', 'auth.principals', 0, '566abcb49c2a858ed0ae776b31aed25ea8ffefa7b9cc209168132adfbc6b50b3'): 'held_PG_B_installer_enrollment; fixed_login_OID/map; caller_SQL_transaction; C06_identity_fact',
    ('schema/auth/007-runtime-grants.sql', 'auth.first_identity', 'INSERT', 'auth.project_grants', 0, '566abcb49c2a858ed0ae776b31aed25ea8ffefa7b9cc209168132adfbc6b50b3'): 'held_PG_B_installer_enrollment; fixed_login_OID/map; caller_SQL_transaction; C06_identity_fact',
    ('schema/auth/007-runtime-grants.sql', 'auth.first_identity', 'INSERT', 'core.payloads', 0, '566abcb49c2a858ed0ae776b31aed25ea8ffefa7b9cc209168132adfbc6b50b3'): 'held_PG_B_installer_enrollment; fixed_login_OID/map; caller_SQL_transaction; C06_identity_fact',
    ('schema/auth/007-runtime-grants.sql', 'coordination.runtime_lease_acquire', 'INSERT', 'coordination.supervisor_leases', 0, '3e9e8dc095d6b2eade27160fe22bd62888fdd67485d72f297211aa4054ae8ae8'): 'held_PG_B_supervisor_lease; fixed_login_OID/map; caller_SQL_transaction/row_locks; noncanonical',
    ('schema/auth/007-runtime-grants.sql', 'coordination.runtime_lease_acquire', 'UPDATE', 'coordination.supervisor_leases', 0, '3e9e8dc095d6b2eade27160fe22bd62888fdd67485d72f297211aa4054ae8ae8'): 'held_PG_B_supervisor_lease; fixed_login_OID/map; caller_SQL_transaction/row_locks; noncanonical',
    ('schema/auth/007-runtime-grants.sql', 'coordination.runtime_lease_release', 'UPDATE', 'coordination.supervisor_leases', 0, '5304fb47e8aad61a71837095056ebe3f205a895b473839bf989c54293dc54087'): 'held_PG_B_supervisor_lease; fixed_login_OID/map; caller_SQL_transaction/row_locks; noncanonical',
    ('schema/auth/007-runtime-grants.sql', 'coordination.runtime_lease_renew', 'UPDATE', 'coordination.supervisor_leases', 0, 'f87135599af7603c960899b7da962a360f18d1de0f4b45b481be3d45f50cad7a'): 'held_PG_B_supervisor_lease; fixed_login_OID/map; caller_SQL_transaction/row_locks; noncanonical',
    ('src/cortex_core/install/owner.py', 'cross_file:_private', 'DYNAMIC', '<dynamic_or_unsupported>', 0, 'af68350c39c6e3a24c3ab604502687e5a81f5183993100f7fc1a2b2ab1319b50'): 'common_owner_private_binding_DATA; no_SQL_or_transaction',
    ('src/cortex_core/install/recovery.py', 'cross_file:_private', 'DYNAMIC', '<dynamic_or_unsupported>', 0, 'ce413e51c87d27396e6a18264c423d084e972fca54cbcd73ee939046a18fb1b4'): 'common_owner_private_binding_DATA; no_SQL_or_transaction',
    ('src/cortex_core/runtime/role_grants.py', '<module>', 'UPDATE', '<unqualified>:<dynamic_or_unsupported>', 0, '6cb78ab1c6bdad2815633292caac2d1fc296f0b416b8b64e867f4563d4d6b1ae'): 'closed_privilege_name_DATA; no_SQL_or_transaction',
    ('src/cortex_core/runtime/role_grants.py', '<module>', 'UPDATE', '<unqualified>:<dynamic_or_unsupported>', 1, '6cb78ab1c6bdad2815633292caac2d1fc296f0b416b8b64e867f4563d4d6b1ae'): 'closed_privilege_name_DATA; no_SQL_or_transaction',
    ('src/cortex_core/runtime/role_grants.py', '_RoleAdapter._apply_grants', 'DYNAMIC', '<dynamic_or_unsupported>', 0, '281bfbdf994b6f26294e7d203219e975c3c2f14c6b02fe48cbfb7cda6c17d799'): 'fixed_P2_finite_prepare_bind; TXN-001_owned_top_level_SQL; HOLD1_v2_if_mandated; native_HELD',
    ('src/cortex_core/runtime/role_grants.py', '_RoleAdapter._apply_grants', 'DYNAMIC', '<dynamic_or_unsupported>', 1, 'c680eabd0ecde4a4e70103c60041b478fa0a475a79a3a557a23356db81144452'): 'fixed_P2_finite_prepare_bind; TXN-001_owned_top_level_SQL; HOLD1_v2_if_mandated; native_HELD',
    ('src/cortex_core/runtime/role_grants.py', '_RoleAdapter._privileges', 'DYNAMIC', '<dynamic_or_unsupported>', 0, 'bcc1067e5aa1d30a2c57375165ce14348c9aeeacf6120797c200adb19c2a3fd8'): 'fixed_P2_finite_catalogue_read; SELECT_only; fixed_connection; no_write_authority',
    ('src/cortex_core/runtime/role_grants.py', '_RoleAdapter._restore_acl', 'DYNAMIC', '<dynamic_or_unsupported>', 0, 'f6eda71b9c6bc63f21bb91e1b44e620d4ea624b5dbb44082cafc14c609289319'): 'fixed_P2_finite_restore; TXN-001_owned_top_level_SQL; compound_material_IO_HELD',
    ('src/cortex_core/runtime/role_grants.py', '_RoleAdapter._restore_acl', 'DYNAMIC', '<dynamic_or_unsupported>', 1, 'c85719eb75e314124b89ab953f03474fbaa03a9160faf861c974012e9a3862bb'): 'fixed_P2_finite_restore; TXN-001_owned_top_level_SQL; compound_material_IO_HELD',
    ('src/cortex_core/runtime/role_grants.py', '_RoleAdapter._restore_acl', 'DYNAMIC', '<dynamic_or_unsupported>', 2, 'bb1ea693ae8a3c10fdd9972e47e3a2783356cd621ccb85b08c2d684df1ca7d5e'): 'fixed_P2_finite_restore; TXN-001_owned_top_level_SQL; compound_material_IO_HELD',
    ('src/cortex_core/runtime/role_grants.py', '_RoleAdapter._restore_defaults', 'DYNAMIC', '<dynamic_or_unsupported>', 0, 'f886339502dee686cd2338837c497bd3fa64784481bb987bcb953542da80ec5b'): 'fixed_P2_finite_restore; TXN-001_owned_top_level_SQL; compound_material_IO_HELD',
    ('src/cortex_core/runtime/role_grants.py', '_RoleAdapter._restore_defaults', 'DYNAMIC', '<dynamic_or_unsupported>', 1, '23712552e36d7ef0e227ce87f25eeb22779108aa77154b2e8a5e9979cf98bc4f'): 'fixed_P2_finite_restore; TXN-001_owned_top_level_SQL; compound_material_IO_HELD',
    ('src/cortex_core/runtime/role_grants.py', '_RoleAdapter.bind', 'DYNAMIC', '<dynamic_or_unsupported>', 0, 'a07dc8eb5407d355b3e2a5ae2ae7904523098d781f477fca5422f37396170cc5'): 'fixed_P2_finite_prepare_bind; TXN-001_owned_top_level_SQL; HOLD1_v2_if_mandated; native_HELD',
    ('src/cortex_core/runtime/role_grants.py', '_RoleAdapter.bind', 'DYNAMIC', '<dynamic_or_unsupported>', 1, 'fc5406b031a27e5d5080b230ded62a011c77ecf5f79acb0211910b117a05397f'): 'fixed_P2_finite_prepare_bind; TXN-001_owned_top_level_SQL; HOLD1_v2_if_mandated; native_HELD',
    ('src/cortex_core/runtime/role_grants.py', '_RoleAdapter.bind', 'DYNAMIC', '<dynamic_or_unsupported>', 2, '6fa11c19cb176c9f2c014967bd5935b4f3b62fa8cf037a3e080328d511277968'): 'fixed_P2_finite_prepare_bind; TXN-001_owned_top_level_SQL; HOLD1_v2_if_mandated; native_HELD',
    ('src/cortex_core/runtime/role_grants.py', '_RoleAdapter.bind', 'INSERT', 'auth.runtime_login_bindings', 0, 'bca3931b7626f04cb4c6970149a4bfcd4776693a6fc1ddfb78fb34e5037ee75b'): 'fixed_P2_finite_prepare_bind; TXN-001_owned_top_level_SQL; HOLD1_v2_if_mandated; native_HELD',
    ('src/cortex_core/runtime/role_grants.py', '_RoleAdapter.bind', 'UPDATE', 'auth.runtime_login_bindings', 0, '5a33d5f7cc7e4b722821bd1c47784d4eb10a9b97498ae727ed1813bb11ff11f8'): 'fixed_P2_finite_prepare_bind; TXN-001_owned_top_level_SQL; HOLD1_v2_if_mandated; native_HELD',
    ('src/cortex_core/runtime/role_grants.py', '_RoleAdapter.close', 'DYNAMIC', '<dynamic_or_unsupported>', 0, 'bcc1067e5aa1d30a2c57375165ce14348c9aeeacf6120797c200adb19c2a3fd8'): 'fixed_P2_finite_catalogue_read; SELECT_only; fixed_connection; no_write_authority',
    ('src/cortex_core/runtime/role_grants.py', '_RoleAdapter.close', 'DYNAMIC', '<dynamic_or_unsupported>', 1, 'bcc1067e5aa1d30a2c57375165ce14348c9aeeacf6120797c200adb19c2a3fd8'): 'fixed_P2_finite_catalogue_read; SELECT_only; fixed_connection; no_write_authority',
    ('src/cortex_core/runtime/role_grants.py', '_RoleAdapter.close', 'DYNAMIC', '<dynamic_or_unsupported>', 2, '1399eb3ef146e097748d7f288531035b01535d5ed015761bdb589aaa02841041'): 'fixed_P2_finite_close; TXN-001_owned_top_level_SQL; compound_backend_effects_HELD',
    ('src/cortex_core/runtime/role_grants.py', '_RoleAdapter.close', 'DYNAMIC', '<dynamic_or_unsupported>', 3, 'bcc1067e5aa1d30a2c57375165ce14348c9aeeacf6120797c200adb19c2a3fd8'): 'fixed_P2_finite_catalogue_read; SELECT_only; fixed_connection; no_write_authority',
    ('src/cortex_core/runtime/role_grants.py', '_RoleAdapter.close', 'DYNAMIC', '<dynamic_or_unsupported>', 4, 'bcc1067e5aa1d30a2c57375165ce14348c9aeeacf6120797c200adb19c2a3fd8'): 'fixed_P2_finite_catalogue_read; SELECT_only; fixed_connection; no_write_authority',
    ('src/cortex_core/runtime/role_grants.py', '_RoleAdapter.prepare', 'DYNAMIC', '<dynamic_or_unsupported>', 0, '47ddff420420252758a88b44521618e6c580bca5681f0a0ffa6d94346e6cdc3b'): 'fixed_P2_finite_prepare_bind; TXN-001_owned_top_level_SQL; HOLD1_v2_if_mandated; native_HELD',
    ('src/cortex_core/runtime/role_grants.py', '_RoleAdapter.restore', 'DELETE', 'auth.runtime_login_bindings', 0, '79f48db751f3cb5107167be6ade2b607ef2a4b0c446647706e7a873271485e9e'): 'fixed_P2_finite_restore; TXN-001_owned_top_level_SQL; compound_material_IO_HELD',
    ('src/cortex_core/runtime/role_grants.py', '_RoleAdapter.restore', 'DYNAMIC', '<dynamic_or_unsupported>', 0, 'ec0f4c01e5a8e0c50f182b56ec3b39538cad464c7430818cfd402dc713db5033'): 'fixed_P2_finite_restore; TXN-001_owned_top_level_SQL; compound_material_IO_HELD',
    ('src/cortex_core/runtime/role_grants.py', '_RoleAdapter.restore', 'DYNAMIC', '<dynamic_or_unsupported>', 1, '28d3eb35be007141e93386db64865987bea065ca6990efb934cb07b0ac2a4010'): 'fixed_P2_finite_restore; TXN-001_owned_top_level_SQL; compound_material_IO_HELD',
    ('src/cortex_core/runtime/role_grants.py', '_RoleAdapter.restore', 'DYNAMIC', '<dynamic_or_unsupported>', 2, 'b6b7e8ccea0b67952297e41407e23e3df7ff0a0e53addb42ce992dd866d5ba0f'): 'fixed_P2_finite_restore; TXN-001_owned_top_level_SQL; compound_material_IO_HELD',
    ('src/cortex_core/runtime/role_grants.py', '_RoleAdapter.restore', 'DYNAMIC', '<dynamic_or_unsupported>', 3, '3d6c64228dc78ce46aa66ef1808cc1117bf6fc4910e849a95b6abe0e487c4beb'): 'fixed_P2_finite_restore; TXN-001_owned_top_level_SQL; compound_material_IO_HELD',
    ('src/cortex_core/runtime/role_grants.py', '_RoleAdapter.restore', 'DYNAMIC', '<dynamic_or_unsupported>', 4, '1a74122bdedbe3d86e1647ed5d0f41d543c89176df2349445bb552043e016fb2'): 'fixed_P2_finite_restore; TXN-001_owned_top_level_SQL; compound_material_IO_HELD',
    ('src/cortex_core/runtime/role_grants.py', '_RoleAdapter.restore', 'DYNAMIC', '<dynamic_or_unsupported>', 5, '43759d47d3c949ad91d9a11cc3e8bb25992598dcff4a1799b3999f055b190129'): 'fixed_P2_finite_restore; TXN-001_owned_top_level_SQL; compound_material_IO_HELD',
    ('src/cortex_core/runtime/role_grants.py', '_RoleAdapter.restore', 'DYNAMIC', '<dynamic_or_unsupported>', 6, '61b41d3b8dc7f7e6df6e14e59fa06358f26b06154a012780da07a7d8ca16f451'): 'fixed_P2_finite_restore; TXN-001_owned_top_level_SQL; compound_material_IO_HELD',
    ('src/cortex_core/runtime/role_grants.py', '_RoleAdapter.restore', 'DYNAMIC', '<dynamic_or_unsupported>', 7, '809d48f45a1cb310d8418cec76e8fcfca8b007072ac3f0bdb8a12283e9b294e8'): 'fixed_P2_finite_restore; TXN-001_owned_top_level_SQL; compound_material_IO_HELD',
    ('src/cortex_core/runtime/role_grants.py', '_RoleAdapter.restore', 'DYNAMIC', '<dynamic_or_unsupported>', 8, 'b03989d6afc2d40a9504d2c80aa2dc787c41890a98abc02dd166ecdd36d42f4f'): 'fixed_P2_finite_restore; TXN-001_owned_top_level_SQL; compound_material_IO_HELD',
    ('src/cortex_core/runtime/role_grants.py', '_RoleAdapter.restore', 'INSERT', 'auth.runtime_login_bindings', 0, '2c511492c2582bdcb002d85452783e9b2057a82a3b82b56830ec88fdb6584729'): 'fixed_P2_finite_restore; TXN-001_owned_top_level_SQL; compound_material_IO_HELD',
    ('src/cortex_core/runtime/role_grants.py', '_RoleAdapter.validate_acl', 'DYNAMIC', '<dynamic_or_unsupported>', 0, '83ee99940a41ec174538b74a80a9982e50a169ce3c647bd967d58fc26873f93e'): 'fixed_P2_finite_catalogue_read; SELECT_only; fixed_connection; no_write_authority',
    ('src/cortex_core/runtime/role_grants.py', 'cross_file:_private', 'DYNAMIC', '<dynamic_or_unsupported>', 0, 'ce413e51c87d27396e6a18264c423d084e972fca54cbcd73ee939046a18fb1b4'): 'common_owner_private_binding_DATA; no_SQL_or_transaction',
    ('src/cortex_core/runtime/role_grants.py', 'cross_file:_private', 'DYNAMIC', '<dynamic_or_unsupported>', 1, 'ce413e51c87d27396e6a18264c423d084e972fca54cbcd73ee939046a18fb1b4'): 'common_owner_private_binding_DATA; no_SQL_or_transaction',
    ('src/cortex_core/runtime/connections.py', 'dynamic_name:call_target', 'DYNAMIC', '<dynamic_or_unsupported>', 0, '75259fd33189c1a457d22bfd6031e45b68492b405f5a582484c3b7b91b8a1a81'): 'X3_fixed_SQLPolicy_field_comparison_DATA; closed_9_field_tuple; no_SQL_or_transaction',
    ('src/cortex_core/runtime/connections.py', 'dynamic_name:call_target', 'DYNAMIC', '<dynamic_or_unsupported>', 1, '8bf1bdd2a518fa8bc709860584a2181498163736ace4974522dc374cbde711cf'): 'X3_fixed_SQLPolicy_field_comparison_DATA; closed_9_field_tuple; no_SQL_or_transaction',
    ('src/cortex_core/runtime/connections.py', 'dynamic_name:getattr', 'DYNAMIC', '<dynamic_or_unsupported>', 0, '75259fd33189c1a457d22bfd6031e45b68492b405f5a582484c3b7b91b8a1a81'): 'X3_fixed_SQLPolicy_field_comparison_DATA; closed_9_field_tuple; no_SQL_or_transaction',
    ('src/cortex_core/runtime/connections.py', 'dynamic_name:getattr', 'DYNAMIC', '<dynamic_or_unsupported>', 1, '8bf1bdd2a518fa8bc709860584a2181498163736ace4974522dc374cbde711cf'): 'X3_fixed_SQLPolicy_field_comparison_DATA; closed_9_field_tuple; no_SQL_or_transaction',
    ('src/cortex_core/runtime/secret_resolver.py', 'dynamic_name:call_target', 'DYNAMIC', '<dynamic_or_unsupported>', 0, '21329c6f12d58a5f62d53b9f7be23d875418554e4aa2c4ed9c66ce80dec15d9f'): 'X3_fixed_Grant_atom_validation_DATA; closed_7_field_tuple; no_SQL_or_transaction',
    ('src/cortex_core/runtime/secret_resolver.py', 'dynamic_name:call_target', 'DYNAMIC', '<dynamic_or_unsupported>', 1, '728f5d8f24222d3e2e5f002195773c70598ee81b8f0805badbd5e03be7733700'): 'X3_fixed_Grant_currentness_comparison_DATA; closed_9_field_tuple; no_SQL_or_transaction',
    ('src/cortex_core/runtime/secret_resolver.py', 'dynamic_name:call_target', 'DYNAMIC', '<dynamic_or_unsupported>', 2, '187355d3936fc21420c4c92169bb2dd9dd23a23793c1b5356c1a50d63d7b2561'): 'X3_fixed_Grant_currentness_comparison_DATA; closed_9_field_tuple; no_SQL_or_transaction',
    ('src/cortex_core/runtime/secret_resolver.py', 'dynamic_name:getattr', 'DYNAMIC', '<dynamic_or_unsupported>', 0, '21329c6f12d58a5f62d53b9f7be23d875418554e4aa2c4ed9c66ce80dec15d9f'): 'X3_fixed_Grant_atom_validation_DATA; closed_7_field_tuple; no_SQL_or_transaction',
    ('src/cortex_core/runtime/secret_resolver.py', 'dynamic_name:getattr', 'DYNAMIC', '<dynamic_or_unsupported>', 1, '728f5d8f24222d3e2e5f002195773c70598ee81b8f0805badbd5e03be7733700'): 'X3_fixed_Grant_currentness_comparison_DATA; closed_9_field_tuple; no_SQL_or_transaction',
    ('src/cortex_core/runtime/secret_resolver.py', 'dynamic_name:getattr', 'DYNAMIC', '<dynamic_or_unsupported>', 2, '187355d3936fc21420c4c92169bb2dd9dd23a23793c1b5356c1a50d63d7b2561'): 'X3_fixed_Grant_currentness_comparison_DATA; closed_9_field_tuple; no_SQL_or_transaction',
}


def normalized_function_digest(function):
    # 3.13 changed ast.dump's default to hide empty fields; keep the 3.12 form.
    options={'show_empty':True} if sys.version_info >= (3,13) else {}
    return hashlib.sha256(ast.dump(function,include_attributes=False,**options).encode()).hexdigest()


def input_bindings_intact(file,tree):
    # Pin renewal is necessary but cannot authorize replacing these bindings.
    names={
        'src/cortex_core/migrations.py': {'prepare_migrations'},
        'src/cortex_core/release/codec.py': {'_validator_class'},
        'src/cortex_core/schema/migrate.py': {'migrations'},
        'src/cortex_core/runtime/catalog.py': {'_rows'},
    }.get(file,set())
    return not any(isinstance(node,ast.Name) and node.id in names
                   and isinstance(node.ctx,(ast.Store,ast.Del)) for node in ast.walk(tree))


def reviewed_input(file,owner,tree,function):
    """Both complete ASTs must still match their source-reviewed versions."""
    return (function is not None and input_bindings_intact(file,tree)
            and normalized_function_digest(tree)==KNOWN_INPUT_FILE_SHA256.get(file)
            and normalized_function_digest(function)==KNOWN_INPUT_FUNCTION_SHA256.get((file,owner)))


CODE_FIELDS = ('co_argcount','co_posonlyargcount','co_kwonlyargcount','co_nlocals',
               'co_stacksize','co_flags','co_code','co_consts','co_names','co_varnames',
               'co_filename','co_name','co_qualname','co_firstlineno','co_linetable',
               'co_exceptiontable','co_freevars','co_cellvars')
REFLECTION_INPUTS = {
    'src/cortex_core/release/_pyc_check.py': ('_compiled_fields_equal',('actual','expected'),'field',CODE_FIELDS),
    'src/cortex_core/release/build/audit.py': ('_code_equal',('left','right'),'k',CODE_FIELDS),
    'src/vector_baseline/product_path.py': ('snapshot_corpus',('staged',),'name',
                                         ('vectors','records','offsets','indices','weights')),
}


def reviewed_reflection_calls(file,tree):
    """Recognize only the exact getters over closed, reviewed field tuples.

    No runtime import or evaluation: the enclosing function and module pins also
    guard type provenance, builtin rebinding and aliases outside the function.
    """
    if file not in REFLECTION_INPUTS:return set()
    owner,objects,variable,fields=REFLECTION_INPUTS[file]
    functions=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==owner]
    if len(functions)!=1 or not reviewed_input(file,owner,tree,functions[0]):return set()
    function=functions[0]
    def field_tuple(node):
        return isinstance(node,ast.Tuple) and tuple(static_text(n) for n in node.elts)==fields
    if fields==CODE_FIELDS:
        assignments=[n for n in ast.walk(function) if isinstance(n,ast.Assign)
                     and len(n.targets)==1 and isinstance(n.targets[0],ast.Name) and n.targets[0].id=='fields']
        if len(assignments)!=1 or not field_tuple(assignments[0].value):return set()
    for expression in ast.walk(function):
        if not isinstance(expression,(ast.GeneratorExp,ast.DictComp)) or len(expression.generators)!=1:continue
        loop=expression.generators[0]
        if (not isinstance(loop.target,ast.Name) or loop.target.id!=variable
                or loop.ifs or loop.is_async):continue
        if fields==CODE_FIELDS:
            if not isinstance(loop.iter,ast.Name) or loop.iter.id!='fields':continue
        elif not field_tuple(loop.iter):continue
        value=expression.elt if isinstance(expression,ast.GeneratorExp) else expression.value
        calls=[n for n in ast.walk(value) if isinstance(n,ast.Call)
               and isinstance(n.func,ast.Name) and n.func.id=='getattr']
        if (len(calls)==len(objects) and all(len(n.args)==2 and not n.keywords
                and isinstance(n.args[0],ast.Name) and n.args[0].id==obj
                and isinstance(n.args[1],ast.Name) and n.args[1].id==variable
                for n,obj in zip(calls,objects))):return set(calls)
    return set()


def reviewed_factory_calls(file,tree):
    """Only codec's reviewed jsonschema class factory, applied to its schema."""
    if file!='src/cortex_core/release/codec.py':return set()
    functions={n.name:n for n in tree.body if isinstance(n,ast.FunctionDef)}
    factory,consumer=functions.get('_validator_class'),functions.get('schema_validate')
    if not all(reviewed_input(file,name,tree,fn) for name,fn in
               [('_validator_class',factory),('schema_validate',consumer)]):return set()
    assignments=[n for n in ast.walk(factory) if isinstance(n,ast.Assign)]
    shapes={ast.unparse(n) for n in assignments}
    expected={"value = globals().get('WireValidator')",
              "value = extend(Draft202012Validator, {'maxLength': _byte_length, 'uniqueItems': _unique_items, 'contains': _contains})",
              "globals()['WireValidator'] = value"}
    returns=[ast.unparse(n) for n in ast.walk(factory) if isinstance(n,ast.Return)]
    imports=[ast.unparse(n) for n in factory.body if isinstance(n,ast.ImportFrom)]
    if (len(assignments)!=3 or shapes!=expected or returns!=['return value']
            or imports!=['from jsonschema import Draft202012Validator',
                         'from jsonschema.exceptions import ValidationError',
                         'from jsonschema.validators import extend']):return set()
    return {n.func for n in ast.walk(consumer) if isinstance(n,ast.Call)
            and ast.unparse(n)=="_validator_class()(schema)"}


def reviewed_migration_references(file,tree):
    """Close only the one forwarding call and the exact mock attribute label."""
    if file=='src/cortex_core/schema/migrate.py':
        functions=[n for n in tree.body if isinstance(n,ast.FunctionDef)
                   and n.name=='apply_forward_migrations']
        if len(functions)!=1:return set()
        fn=functions[0]
        if (not reviewed_input(file,fn.name,tree,fn) or len(fn.body)!=1
                or ast.unparse(fn.body[0])!='return migrations.apply_migrations(connection, directory)'):
            return set()
        return {fn.body[0].value.func}
    if file=='src/cortex_core/schema/tests/test_readiness.py':
        classes=[n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='ReadinessTests']
        functions=[n for cls in classes for n in cls.body if isinstance(n,ast.FunctionDef)
                   and n.name=='test_existing_runner_is_only_migration_engine']
        if len(functions)!=1:return set()
        fn=functions[0]
        if not reviewed_input(file,'ReadinessTests.'+fn.name,tree,fn):return set()
        return {n.args[1] for n in ast.walk(fn) if isinstance(n,ast.Call)
                and ast.unparse(n)=="patch.object(migrations, 'apply_migrations', return_value=['core-0001'])"}
    return set()


def source_reference_flags(file,source):
    """Unreviewed exempt-name references and dynamic reflection in NEXT source."""
    tree=ast.parse(source);flags=[];ordinals={}
    reviewed_calls=reviewed_reflection_calls(file,tree) | reviewed_factory_calls(file,tree)
    reviewed_references=reviewed_migration_references(file,tree)
    call_targets={node.func for node in ast.walk(tree) if isinstance(node,ast.Call)}
    getter_references={node for node in ast.walk(tree) if (
        isinstance(node,ast.Name) and node.id=='getattr'
        or isinstance(node,ast.alias) and node.name=='getattr'
        or isinstance(node,ast.Attribute) and node.attr=='getattr'
        or isinstance(node,ast.Subscript) and static_text(node.slice)=='getattr')}
    def flag(kind,node):
        ordinal=ordinals.get(kind,0);ordinals[kind]=ordinal+1
        flags.append(dict(file=file,owner=kind,verb='DYNAMIC',table='<dynamic_or_unsupported>',
                          ordinal=ordinal,statement_sha256=hashlib.sha256(
                              ast.dump(node,include_attributes=False).encode()).hexdigest(),classification=None))
    for node in ast.walk(tree):
        symbols=[]
        if isinstance(node,ast.Name):symbols=[node.id]
        elif isinstance(node,ast.Attribute):symbols=[node.attr]
        elif isinstance(node,ast.alias):symbols=[node.name,node.asname]
        elif isinstance(node,ast.Constant) and isinstance(node.value,str):symbols=[node.value]
        for symbol in symbols:
            if (symbol in EXEMPT_SYMBOL_FILES and file not in EXEMPT_SYMBOL_FILES[symbol]
                    and node not in reviewed_references):
                flag('cross_file:'+symbol,node)
        if node in getter_references and node not in call_targets:
            flag('dynamic_name:getattr_alias',node)
        if isinstance(node,ast.Call):
            function=node.func;index=None;label=None
            # OI-REFLECT-001: a literal method name does not close the callable
            # it returns. Flag invocation (even with opaque SQL), or an escaping
            # execute/executemany or unresolved-name getter, without evaluating
            # receiver/arguments. A local attribute alias is not a closed name.
            if ((isinstance(function,ast.Call) and function not in reviewed_calls)
                    or (function in getter_references
                        and node not in reviewed_calls and node not in call_targets
                        and len(node.args)>1
                        and static_text(node.args[1]) in (None,'execute','executemany'))):
                flag('dynamic_name:call_target',node)
            if isinstance(function,ast.Name) and function.id in ('getattr','setattr','exec','eval','__import__'):
                label=function.id;index=1 if label in ('getattr','setattr') else 0
            elif (isinstance(function,ast.Attribute) and function.attr=='import_module'
                  and isinstance(function.value,ast.Name) and function.value.id=='importlib'):
                label='importlib.import_module';index=0
            if (label is not None and node not in reviewed_calls
                    and (len(node.args)<=index or static_text(node.args[index]) is None)):
                flag('dynamic_name:'+label,node)
    return flags


def static_text(node):
    """Fold source-known SQL without executing application expressions."""
    if isinstance(node,ast.Constant) and isinstance(node.value,str):return node.value
    if isinstance(node,ast.BinOp) and isinstance(node.op,ast.Add):
        left,right=static_text(node.left),static_text(node.right)
        if left is not None and right is not None:return left+right
    if isinstance(node,ast.JoinedStr):
        parts=[static_text(part) for part in node.values]
        if all(part is not None for part in parts):return ''.join(parts)
    return None


def category(file,owner,verb,table):
    if table.startswith('retrieval.') and ('embeddings/' in file or 'modules/graph/' in file or file.startswith('schema/retrieval/')):
        return 'derived_projection_or_cache; separate module admission held'
    if table=='coordination.supervisor_leases' and ('conductor/' in file or 'c08-supervisor' in file):
        return 'optional_C08_control_plane; outside canonical mutation feed'
    if file=='src/cortex_core/migrations.py' and table=='core.schema_migrations' and verb=='INSERT':
        return 'explicit_installer_only; never runtime mutation'
    if file=='src/cortex_core/records.py':
        rules={('_payload','core.payloads','INSERT'):'immutable_payload_subordinate',
          ('_append_revision','core.record_revisions','INSERT'):'trigger_capture_and_deferred_history_guard',
          ('_save_request','coordination.idempotency','INSERT'):'immutable_current_event_receipt_guard',
          ('Records._mutate','core.records','INSERT'):'trigger_capture_and_sequential_head_guard',
          ('Records._mutate','core.records','UPDATE'):'trigger_capture_and_sequential_head_guard'}
        return rules.get((owner,table,verb))
    if file=='src/cortex_core/coordination.py':
        allowed={('_append_result','coordination.job_results','INSERT'),('Jobs.create.create','coordination.jobs','INSERT'),
          ('Jobs.claim.claim','coordination.leases','INSERT'),('Jobs.claim.claim','coordination.job_attempts','INSERT'),
          ('Jobs._state','coordination.jobs','UPDATE'),('Jobs._state','coordination.leases','UPDATE'),
          ('Jobs._replace_metadata','coordination.jobs','UPDATE')}
        if (owner,table,verb) in allowed:return 'job_parent_snapshot_before_receipt; deferred_final_snapshot_guard'
    if file=='schema/coordination/002-record-replay.sql' and owner=='coordination.c05_update_head' and table=='core.records' and verb=='UPDATE':
        return 'trigger_capture_and_sequential_head_guard'
    if file=='schema/auth/002-isolation.sql' and owner=='auth.bind_scope' and table=='pg_temp.c04_request_context':
        return 'private_transaction_binding_housekeeping'
    if file=='schema/auth/002-isolation.sql' and owner=='auth.bump_generation' and table=='auth.permission_generations':
        return 'authorization_generation_housekeeping'
    if file=='schema/auth/003-identity.sql':
        if owner in ('auth.identity_register','auth.identity_set_roles','auth.identity_rotate','auth.identity_revoke','auth.identity_adopt') and table in ('auth.principals','auth.project_grants','auth.credentials'):
            return 'fixed_identity_authority; captured_by_additive_identity_finish'
        if owner=='auth.identity_finish' and table in ('core.payloads','coordination.idempotency'):
            return 'original_immutable_audit_and_receipt; additive_finish_captures_every_target'
    if file=='schema/auth/004-first-identity-capture.sql':
        # Additive replacements of the existing C06 capture/fact functions.
        if (owner,verb,table)==('coordination.c06_capture_revision','INSERT','coordination.outbox'):
            return 'private_C06_c06_capture_revision'
        if owner=='coordination.c06_fact' and (verb,table) in {
                ('INSERT','core.records'),('INSERT','core.record_aliases'),
                ('UPDATE','core.records'),('INSERT','core.record_revisions')}:
            return 'private_C06_c06_fact'
    if (file=='schema/auth/005-first-identity.sql' and owner=='auth.first_identity'
            and verb=='INSERT' and table in {'auth.principals','auth.project_grants',
                                             'auth.credentials','auth.first_identity_receipts','core.payloads'}):
        return 'explicit_installer_only; never runtime mutation'
    if (file=='schema/coordination/005-session-source.sql' and owner=='coordination.c11b3_session_claim'
            and verb=='INSERT' and table=='coordination.session_sources'):
        return 'private_transaction_binding_housekeeping'
    if file=='schema/coordination/002-outbox.sql':
        rules={'coordination.c06_capture_revision':{'coordination.outbox'},
          'coordination.c06_fact':{'core.records','core.record_aliases','core.record_revisions'},
          'coordination.capture_job':{'core.payloads'},
          'auth.identity_finish':{'core.payloads','coordination.idempotency'},
          'coordination.publish_outbox':{'coordination.feed_state','coordination.published_events'},
          'coordination.prune_outbox':{'coordination.feed_state','coordination.consumer_checkpoints','coordination.published_events','coordination.quarantine','coordination.outbox'},
          'coordination.c06_protection_lock':{'coordination.feed_state'}}
        if table in rules.get(owner,set()):return 'private_C06_'+owner.rsplit('.',1)[-1]
    if file=='schema/coordination/003-module-consumers.sql':
        rules={
          'coordination.c07_register':{'coordination.feed_state','coordination.consumer_project_checkpoints',
            'coordination.consumer_checkpoints'},
          'coordination.c07_expire':{'coordination.consumer_project_checkpoints'},
          'coordination.c07_record':{'coordination.consumer_aggregate_heads',
            'coordination.consumer_event_outcomes','coordination.quarantine',
            'coordination.consumer_project_checkpoints'},
          'coordination.c07_scan':{'coordination.consumer_project_checkpoints'}}
        if table in rules.get(owner,set()):
            return 'private_C07_scoped_durable_outcome_after_target_receipt'
    return None


CATALOG_QUERY_SHA256 = {
    '29b7135ce57852eab197af92e82a885cd964e75ed239005e3563ff2cbcbf478d',
    '4cdbd04649b4ee5c673aa7fe12d5f89f6fcb3ebe03bc10013c016f4239867966',

    '0270678748d0ce1314395c71e7e2266b110ba28a5804dde753c7f27bb4b1de5b',
    '14ae809f195f5e037d100ccf3c9dbec7f26cf168f9c918702a4735aee5226058',
    '9188618b95ecb6d143d06d837854e1f27d84283ce1f4188e493e57d80f12efcc',
    'aaaefdc0752541f7a5bd82589919149273d995f228ab4cd1cbf32cd77957fee9',
    'c6e82cd45986e1935eda39349b83504cdb945bcba7b4b7c0d2fa8d731a2883de',
    'dbc994ef01a82d7c4f062343e4d2dc94c2c0034561c410d057f2164f98ca014e',
}


def reviewed_catalog_inputs(tree):
    """The sole helper has eight fixed reviewed SELECT inputs and no escaped alias."""
    calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call)
           and isinstance(n.func,ast.Name) and n.func.id=='_rows']
    references=[n for n in ast.walk(tree) if isinstance(n,ast.Name) and n.id=='_rows']
    if len(calls)!=8 or set(references)!={call.func for call in calls}:return False
    queries=[static_text(call.args[1]) if len(call.args) in (2,3) else None for call in calls]
    return (all(isinstance(sql,str) and hashlib.sha256(sql.encode()).hexdigest() in CATALOG_QUERY_SHA256
                for sql in queries) and len(set(queries))==8
            and all(isinstance(call.args[0],ast.Name) and call.args[0].id=='connection'
                    and not call.keywords for call in calls))


def scan_source(file,source):
    rows=[]; ordinals={}
    def add(owner,text):
        for match in DML.finditer(text):
            verb=match.group(1).split()[0].upper()
            if text[match.start()-1:match.start()]=="'" and text[match.end():match.end()+1]=="'":continue
            if verb=='UPDATE' and re.search(r'\bFOR\s*$',text[:match.start()],re.I):continue
            target=TARGET.match(text,match.end())
            table='<dynamic_or_unsupported>' if target is None else re.sub(r'\s+','',target.group(1)).replace('"','').lower()
            if '.' not in table:table='<unqualified>:'+table
            key=(owner,verb,table);ordinal=ordinals.get(key,0);ordinals[key]=ordinal+1
            normalized=' '.join(text.split())
            rows.append(dict(file=file,owner=owner,verb=verb,table=table,ordinal=ordinal,
                statement_sha256=hashlib.sha256(normalized.encode()).hexdigest(),classification=category(file,owner,verb,table)))
    def add_dynamic(owner,node,force=False):
        fragments=[child.value for child in sorted(ast.walk(node),key=lambda part:(getattr(part,'lineno',0),getattr(part,'col_offset',0)))
                   if isinstance(child,ast.Constant) and isinstance(child.value,str)]
        candidate=''.join(fragments)
        if not force and not any(not (match.group().upper()=='UPDATE' and re.search(r'\bFOR\s*$',candidate[:match.start()],re.I))
                   for match in re.finditer(r'\b(?:INSERT|UPDATE|DELETE)\b',candidate,re.I)):return
        key=(owner,'DYNAMIC','<dynamic_or_unsupported>');ordinal=ordinals.get(key,0);ordinals[key]=ordinal+1
        rows.append(dict(file=file,owner=owner,verb='DYNAMIC',table='<dynamic_or_unsupported>',ordinal=ordinal,
            statement_sha256=hashlib.sha256(ast.dump(node,include_attributes=False).encode()).hexdigest(),classification=None))
    def known_existing_input(owner,node,tree,function):
        """Only these source-proved existing indirect inputs bypass fail-closed."""
        if not reviewed_input(file,owner,tree,function):return False
        if file=='src/cortex_core/coordination.py' and owner=='Jobs._load':
            return (isinstance(node,ast.BinOp) and isinstance(node.op,ast.Add)
                    and (static_text(node.left) or '').lstrip().upper().startswith('SELECT')
                    and isinstance(node.right,ast.IfExp)
                    and static_text(node.right.body)==' FOR UPDATE'
                    and static_text(node.right.orelse)=='')
        if not isinstance(node,ast.Name):return False
        name=node.id
        if file=='src/cortex_core/runtime/catalog.py' and owner=='_rows' and name=='sql':
            return (ast.unparse(function)=="def _rows(connection, sql, parameters=None):\n    return connection.execute(sql, parameters).fetchall()"
                and reviewed_catalog_inputs(tree))
        if file=='src/cortex_core/backup_validation.py' and owner=='read_restored_snapshot' and name=='SNAPSHOT_SQL':
            assignments=[n for n in tree.body if isinstance(n,ast.Assign) and len(n.targets)==1
                         and isinstance(n.targets[0],ast.Name) and n.targets[0].id==name]
            stores=[n for n in ast.walk(tree) if isinstance(n,ast.Name) and n.id==name
                    and isinstance(n.ctx,(ast.Store,ast.Del))]
            query=static_text(assignments[0].value) if len(assignments)==1 else None
            return (len(assignments)==1 and len(stores)==1 and stores[0] is assignments[0].targets[0]
                    and query is not None and query.lstrip().upper().startswith('SELECT')
                    and not DML.search(query))
        if name=='_ROLE_QUERY' and file in ('src/cortex_core/auth.py','src/cortex_core/identity.py'):
            assignments=[n for n in ast.walk(tree) if isinstance(n,(ast.Assign,ast.AnnAssign))
                         and any(isinstance(t,ast.Name) and t.id==name for t in
                                 (n.targets if isinstance(n,ast.Assign) else [n.target]))]
            all_stores=[n for n in ast.walk(tree) if isinstance(n,ast.Name) and n.id==name
                        and isinstance(n.ctx,(ast.Store,ast.Del))]
            declarations=[n for n in ast.walk(tree) if isinstance(n,(ast.Global,ast.Nonlocal))
                          and name in n.names]
            if declarations:return False
            if file.endswith('/auth.py'):
                return (len(assignments)==len(all_stores)==1
                        and all_stores[0] is assignments[0].targets[0]
                        and (static_text(assignments[0].value) or '').lstrip().upper().startswith('SELECT'))
            return (not assignments and not all_stores and any(isinstance(n,ast.ImportFrom) and n.module=='auth'
                        and any(a.name=='_ROLE_QUERY' for a in n.names) for n in tree.body))
        if file=='src/cortex_core/identity.py' and owner=='Identity._register' and name=='query':
            methods=[n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='_register']
            assignments=[n for n in ast.walk(methods[0]) if isinstance(n,ast.Assign)
                         and any(isinstance(t,ast.Name) and t.id=='query' for t in n.targets)] if len(methods)==1 else []
            if len(methods)!=1:return False
            return (len(assignments)==2
                    and all((static_text(n.value) or '').lstrip().upper().startswith('SELECT') for n in assignments))
        if file=='src/cortex_core/records.py' and owner=='_private' and name=='query':
            methods=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_private']
            if len(methods)!=1:return False
            function=methods[0]
            parameters=[arg.arg for arg in (function.args.posonlyargs+function.args.args+
                                           function.args.kwonlyargs)]
            calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)
                   and n.func.id=='_private']
            return parameters.count(name)==1 and bool(calls) and all(len(n.args)>1 and
                   (static_text(n.args[1]) or '').lstrip().upper().startswith('SELECT') for n in calls)
        if file=='src/cortex_core/migrations.py' and owner=='apply_migrations' and name=='sql':
            functions=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='apply_migrations']
            if len(functions)!=1:return False
            fn=functions[0]
            loops=[n for n in ast.walk(fn) if isinstance(n,ast.For) and isinstance(n.target,ast.Tuple)
                   and [getattr(t,'id',None) for t in n.target.elts]==['identity','digest','sql']
                   and ast.unparse(n.iter)=='prepared[:prepared_target + 1]']
            helpers=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='prepare_migrations']
            if len(helpers)!=1 or not reviewed_input(file,'prepare_migrations',tree,helpers[0]):return False
            helper=helpers[0]
            bindings=[n for n in ast.walk(fn) if isinstance(n,ast.Assign)
                      and any(isinstance(t,ast.Name) and t.id=='prepared' for t in n.targets)]
            if len(bindings)!=1 or ast.unparse(bindings[0])!='prepared = prepare_migrations(directory)':return False
            if [ast.unparse(n) for n in ast.walk(helper) if isinstance(n,ast.Return)]!=['return prepared']:return False
            appends=[n for n in ast.walk(helper) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute)
                     and n.func.attr=='append' and isinstance(n.func.value,ast.Name) and n.func.value.id=='prepared']
            other_assignments=[n for n in ast.walk(fn) if isinstance(n,(ast.Assign,ast.AnnAssign,ast.NamedExpr))
                               and any(isinstance(t,ast.Name) and t.id=='sql' for t in
                                       (n.targets if isinstance(n,ast.Assign) else [n.target]))]
            return (len(loops)==1 and len(appends)==1 and not other_assignments
                    and 'hashlib.sha256(data).hexdigest() != entry["sha256"]' in source
                    and ast.unparse(appends[0].args[0])=="(entry['id'], entry['sha256'], data.decode('utf-8'))")
        return False
    if file.endswith('.py'):
        tree=ast.parse(source)
        class Visitor(ast.NodeVisitor):
            def __init__(self):self.owners=[];self.functions=[]
            def visit_ClassDef(self,node):
                self.owners.append(node.name);self.generic_visit(node);self.owners.pop()
            def visit_FunctionDef(self,node):
                self.owners.append(node.name);self.functions.append(node)
                self.generic_visit(node)
                self.functions.pop();self.owners.pop()
            visit_AsyncFunctionDef=visit_FunctionDef
            def visit_Call(self,node):
                if (isinstance(node.func,ast.Attribute) and node.func.attr in ('execute','executemany')
                    and node.args and static_text(node.args[0]) is None):
                    owner='.'.join(self.owners) or '<module>'
                    if not known_existing_input(owner,node.args[0],tree,self.functions[-1] if self.functions else None):
                        add_dynamic(owner,node.args[0],force=True)
                self.generic_visit(node)
            def visit_BinOp(self,node):
                value=static_text(node)
                if value is not None:
                    add('.'.join(self.owners) or '<module>',value)
                else:self.generic_visit(node)
            def visit_JoinedStr(self,node):
                value=static_text(node)
                if value is not None:
                    add('.'.join(self.owners) or '<module>',value)
                else:self.generic_visit(node)
            def visit_Constant(self,node):
                if isinstance(node.value,str):add('.'.join(self.owners) or '<module>',node.value)
        Visitor().visit(tree)
    else:
        for function in FUNCTION.finditer(source):add(function.group(1),function.group(2))
    return rows


def scan(root,overrides=None):
    root=Path(root);overrides=overrides or {};rows=[];source_hashes={}
    paths=sorted(list((root/'src/cortex_core').rglob('*.py'))+list((root/'schema').rglob('*.sql')))
    for path in paths:
        name=str(path.relative_to(root));source=overrides.get(name,path.read_text())
        source_hashes[name]=hashlib.sha256(source.encode()).hexdigest()
        rows.extend(scan_source(name,source))
    for path in sorted((root/'src').rglob('*.py')):
        name=str(path.relative_to(root));rows.extend(source_reference_flags(name,overrides.get(name,path.read_text())))
    required_sources={key[0] for key in REVIEWED_WRITER_CLASSES} | {
        'schema/auth/005-first-identity.sql','src/cortex_core/install/lifecycle.py'}
    reviewed_sources=(set(REVIEWED_WRITER_SOURCE_SHA256)==required_sources
        and all(source_hashes.get(name)==digest
                for name,digest in REVIEWED_WRITER_SOURCE_SHA256.items()))
    for row in rows:
        if row['classification'] is None and reviewed_sources:
            key=tuple(row[k] for k in ('file','owner','verb','table','ordinal','statement_sha256'))
            row['classification']=REVIEWED_WRITER_CLASSES.get(key)
    return sorted(rows,key=lambda r:(r['file'],r['owner'],r['verb'],r['table'],r['ordinal']))


def audit(root,inventory=None,overrides=None):
    root=Path(root);inventory=inventory if inventory is not None else json.loads((root/'contracts/core-writer-inventory.json').read_bytes())
    actual=scan(root,overrides);declared=inventory.get('writer_sites',[])
    unclassified=[r for r in actual if r['classification'] is None]
    missing=[r for r in actual if r not in declared];stale=[r for r in declared if r not in actual]
    return dict(passed=bool(actual) and not unclassified and not missing and not stale and inventory.get('unclassified_writers')==[],
        writer_count=len(actual),unclassified=unclassified,missing=missing,stale=stale)


if __name__=='__main__':
    root=Path(__file__).resolve().parents[1]
    if '--bind' in sys.argv:
        p=root/'contracts/core-writer-inventory.json';inventory=json.loads(p.read_bytes());inventory['writer_sites']=scan(root)
        inventory['unclassified_writers']=[r for r in inventory['writer_sites'] if r['classification'] is None]
        p.write_text(json.dumps(inventory,indent=2)+'\n')
    result=audit(root);print(json.dumps(result));raise SystemExit(0 if result['passed'] else 1)
