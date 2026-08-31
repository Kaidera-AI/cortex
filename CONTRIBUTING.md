# Contributing to Cortex

Thanks for wanting to help. Until v0.1.0 lands (extraction from Kaidera OS is in progress),
the most useful contributions are design review, issues against the contract in `docs/`, and
roadmap discussion.

## Ground rules

- **Be respectful.** See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
- **No secrets in code, ever.** Not in tests, not in fixtures, not in history.
- **Conventional commits** (`fix:`, `feat:`, `docs:`, `test:` …), one concern per commit.
- **MIT** — by contributing you agree your work is MIT-licensed.

## The quality bar

These are not aspirations; each one exists because its absence cost real production hours:

1. **A green suite is not evidence.** Structural tests pass on broken systems. Behavioural
   claims need a behavioural test — ideally against a real engine.
2. **Prove a test by breaking the code.** Revert the fix, watch the test fail, restore it.
   A test that never failed proves nothing.
3. **Verify the effect, never the declaration.** Asserting config exists is worthless;
   assert the config is *consumed* and the behaviour *changed*.
4. **One owner per fact.** If two components can both mutate a fact, the design is wrong
   before the code is.

## Pull requests

- Small and single-concern beats large and mixed. Always.
- Say what you measured, not what you believe. "Tested on podman 5.7.0, 1 network before,
  0 after" beats "should work".
- Expect adversarial review — a reviewer whose job is to break your claim. It is not
  personal; it is the process that keeps this codebase honest.

## Versioning

Semver, `0.1.x` until the contract stabilises. Breaking API/CLI changes bump minor
pre-1.0 and are called out in [CHANGELOG.md](CHANGELOG.md).
