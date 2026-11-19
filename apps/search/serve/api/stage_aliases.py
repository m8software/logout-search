"""Stage versions that are declared answer-identical to an earlier one (warm-up, 2026-11-17).

A stage's code version is the hash of the source it can reach, and for a local module the hash of the WHOLE FILE. A change to how the engine
starts and loads (the page-id map kept in a mapped file, a read-through string dictionary, the channel index kept on disk, the IVF prefetch, the
start trace's hooks) moves those hashes although no answer changes; a version change makes the builder redo every stored part (a day of work on
300,000 answers). Here, each version the start-up work produced maps to the version it replaces: {stage: {new version: earlier version}}.

The claim behind every line is checked, not assumed: the same 2,036 names, the golden set and the eval sets through a release-2 engine and this
one give identical cards, lists and places (docs/internal/warmup-plan.md, "Proof"). An alias holds for exactly the hash it names: the next change
to anything a stage reaches gives a new version that is in no table, and so counts as a real change. Add a line only with that proof."""
# Engine cut cards-once + list-4s (2026-11-19): the lead and picture code moved out of the card stage into card_text.py
# (cards once; the lead rule with it), the card set, the intake off the store lock, the how-to block's
# cache and pooled connections, comments. No stage output changes apart from the card's lead, which is no longer the card
# stage's (it is the card set's, joined at serve): the 2,036-name compares in docs/internal/precompute-plan.md, "Cards once".
# Warm-yield on pool workers (2026-11-19): a warm request no longer sleeps inside a pool worker while a visitor is in flight (_in_pool
# in search_api.py). That changes when a warm request's lists run, never what they return: a request without a concurrent visitor runs
# the same lines as before, compared on 200 names old against new (commit message).
ALIASES: dict = {
    "base": {"74b2aeaf04a6fcb3": "e2325b55033fdb9a", "93460e00dd408439": "e2325b55033fdb9a", "a760a54a7a8a8a9c": "e2325b55033fdb9a"},
    "card": {"d3842b16a8c46948": "671ccb86ceecf0b6"},
    "list": {"ffe5ed57d22e626d": "0f17c54fbb9ea39a", "490986031faa8a60": "0f17c54fbb9ea39a", "08831ad085a464c4": "0f17c54fbb9ea39a", "c026630617bb62ff": "0f17c54fbb9ea39a"},
    "local": {"b7c6b8d6b2830e88": "0addcc026a55cddc", "8e17a676e72f2ecb": "0addcc026a55cddc"},
    "place": {"c48a8bfc2551fb3d": "4d03ff72698b131c", "ded2c564b2df8a71": "4d03ff72698b131c"},
    "rest": {"672cd6f13db03b91": "0189e80d9f1f89be"},
}
