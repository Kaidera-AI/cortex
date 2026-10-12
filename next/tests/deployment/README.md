# H-D538 U1 deployment runtime-policy contract

The canonical schema is `next/contracts/deployment/runtime-policy.schema.json`.
This deployment contract is separate from R01's artifact `release_policy`.
KOS mechanically vendors identical schema and vector bytes.

`runtime-policy-vectors.json` freezes three positive shapes, nineteen shape
refusals and six shape-valid **required receiver refusals**. The latter cover the
6.0.2 baseline, denylist as-of/order/uniqueness, UTF-8 byte bound and raw numeric
1.0. U2 must run its actual raw/semantic reader against these exact vectors.
The U1 tests prove schema/data identity and shape boundaries, not that a reader,
signature/native observation, profile or runtime has been admitted.

The separate KOS legacy oracle was frozen against current original production
functions before the schema addition. Its twenty-six scenarios use fake IO,
verification, HTTP and command ports, with a byte-preservation assertion and
sixteen source-omission BODY controls. It is not native or crypto proof.

No engine, VM, signing, host installation or production source predicate changed.
