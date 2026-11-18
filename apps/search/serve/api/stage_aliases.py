"""Stage versions that are declared answer-identical to an earlier one (warm-up, 2026-11-17).

A stage's code version is the hash of the source it can reach, and for a local module the hash of the WHOLE FILE. A change to how the engine
starts and loads (the page-id map kept in a mapped file, a read-through string dictionary, the channel index kept on disk, the IVF prefetch, the
start trace's hooks) moves those hashes although no answer changes; a version change makes the builder redo every stored part (a day of work on
300,000 answers). Here, each version the start-up work produced maps to the version it replaces: {stage: {new version: earlier version}}.

The claim behind every line is checked, not assumed: the same 2,036 names, the golden set and the eval sets through a release-2 engine and this
one give identical cards, lists and places (docs/internal/warmup-plan.md, "Proof"). An alias holds for exactly the hash it names: the next change
to anything a stage reaches gives a new version that is in no table, and so counts as a real change. Add a line only with that proof."""
ALIASES: dict = {
    "base": {"1b39b2e447f71ac2": "e2325b55033fdb9a"},
    "card": {"1300ba8661ecbb9a": "ea342477993a6010"},
    "list": {"08831ad085a464c4": "0f17c54fbb9ea39a"},
    "local": {"9efa1dc8a4813ecf": "0addcc026a55cddc"},
    "place": {"c48a8bfc2551fb3d": "4d03ff72698b131c"},
}
