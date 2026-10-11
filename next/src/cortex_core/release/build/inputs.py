"""Direct R01 consumer, with no private schema or field translation."""


def admit(raw, input_reader, accepted_policy):
    from cortex_core.release.inputs import parse_build_input, admit_build_input

    return admit_build_input(parse_build_input(raw), input_reader, accepted_policy)
