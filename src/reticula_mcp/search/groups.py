"""A big hub as lettered groups by subject, A to Z.

``get`` on a hub with more than ``FLAT`` notes in it shows groups instead of
the newest-first page. Each group has a letter, three words, and its size;
the assistant drills with ``get(ref=<hub>, part="AB")``. A group of at most
``FLAT`` notes is listed as the hub's members are; a bigger one is split
again.

Nothing is stored. The groups are computed on each read, from the notes the
viewer may read (the caller hands them in), so two people can see different
letters for the same hub. The same notes give the same groups: every seed
comes from the sorted note ids. A note written between two calls can move the
letters, which is why the answer says to keep a note's id, not its letters.

How a set is split (the rule precis measured and Reto chose, balance first):

- Into ``k = min(26, ceil(n / GROUP_SIZE))`` groups whose sizes differ by one
  at most: ``n mod k`` groups of ``q + 1`` and the rest of ``q``.
- Features: the stored vector of each note (the current model, the head
  version) when every note has one; else the words of title and text for
  all (TF-IDF): the indexer is behind, or the hash embedder of the tests. A
  mix would move the groups each time a vector arrives; this way they change
  once, and the answer says which it is.
- k-means on the unit vectors, seeded by k-means++, where each round assigns
  under the quotas: every pair of note and group by similarity, best first, a
  pair taken while the note is free and the group has room.

Labels: per group the three words that set it apart from its sibling groups
(class-based TF-IDF, as precis ``ctfidf_words``), counted once per note, from
the titles and texts of the group's own notes. A word is lowercase letters
only, 3 to 20 of them, and no stop word or request word: a label can hold
nothing but single words, and no word comes from a note outside the set.
"""

from __future__ import annotations

import hashlib
import math
import re
import string
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from reticula_mcp.accounts import Account
from reticula_mcp.config import Settings
from reticula_mcp.db.pool import Conn
from reticula_mcp.errors import BAD_INPUT, ReticulaError
from reticula_mcp.permissions import readable_hub_ids
from reticula_mcp.search import find
from reticula_mcp.search.embedder import get_embedder
from reticula_mcp.search.find import REQUEST_WORDS

#: A hub with at most this many notes is listed as it is; so is a group.
FLAT = 26
#: About this many notes per group; at most ``len(LETTERS)`` groups.
GROUP_SIZE = 8
LETTERS = string.ascii_uppercase
#: Words per label.
LABEL_WORDS = 3
#: More notes than this: the newest of them are grouped, and the answer says so.
MOST = 5_000
#: Characters of a note's text that its words are taken from.
TEXT_CHARS = 2_000
#: Words kept as features of the lexical form.
VOCABULARY = 1_000
ROUNDS = 12
#: Starts of the k-means of the first split; the best is kept. One below it.
TRIES = 2

_WORD = re.compile(r"[a-z]+")

STOP_WORDS = frozenset(
    """
    a about above after again against all also although always am among an and
    another any are around as at be because been before being below between
    both but by can cannot could did do does doing done down during each either
    else even ever every few for from further get gets got had has have having
    he her here hers herself him himself his how however if in into is it its
    itself just last least less let like made make many may me might more most
    much must my myself near need needs neither never new next no nor not now
    of off often on once one only onto or other others our ours ourselves out
    over own per perhaps put quite rather really said same say says see seem
    seems several shall she should since so some something sometimes still
    such than that the their theirs them themselves then there therefore these
    they thing things this those though three through thus to together too
    toward towards two under until up upon us use used uses using very via was
    way we well were what whatever when where whether which while who whom whose
    why will with within without would yes yet you your yours yourself
    yourselves able across actually already back come could does going good
    great know look looks much okay right take taken think want wants went
    """.split()  # noqa: SIM905 (a list of 230 strings reads worse)
)


@dataclass(frozen=True)
class Note:
    """One note to group: its id, its words, and its vector if it has one."""

    id: int
    text: str
    vector: Sequence[float] | np.ndarray | None = None


@dataclass
class Group:
    """A set of notes: the whole hub (``path`` empty) or a lettered group."""

    path: str
    ids: tuple[int, ...]  # in the order handed in (newest first)
    label: tuple[str, ...] = ()
    children: list[Group] = field(default_factory=list)

    def find(self, part: str) -> Group | None:
        """The group at the letters ``part``, or None."""
        group: Group | None = self
        for letter in part:
            if group is None:
                return None
            group = next((c for c in group.children if c.path[-1] == letter), None)
        return group

    def letters(self) -> str:
        return "".join(c.path[-1] for c in self.children)


def words_of(text: str) -> list[str]:
    """The words a label and the lexical form use, in order."""
    return [
        w
        for w in _WORD.findall(text[:TEXT_CHARS].lower())
        if 3 <= len(w) <= 20 and w not in STOP_WORDS and w not in REQUEST_WORDS
    ]


def _seed(ids: Sequence[int]) -> int:
    data = ",".join(str(i) for i in sorted(ids)).encode()
    return int.from_bytes(hashlib.sha256(data).digest()[:8], "big")


def _unit(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


def _plural_map(docs: Sequence[set[str]]) -> dict[str, str]:
    """``kilns`` counts as ``kiln`` when both occur."""
    seen: set[str] = set().union(*docs) if docs else set()
    return {w: w[:-1] for w in seen if w.endswith("s") and w[:-1] in seen}


def _lexical(docs: Sequence[set[str]]) -> np.ndarray:
    """TF-IDF over the words of each note (each word once), unit rows."""
    n = len(docs)
    df: Counter[str] = Counter()
    for d in docs:
        df.update(d)
    usable = [w for w, c in df.items() if c >= 2 and c < n]
    usable.sort(key=lambda w: (-df[w], w))
    vocab = {w: i for i, w in enumerate(usable[:VOCABULARY])}
    mat = np.zeros((n, max(1, len(vocab))), dtype=np.float32)
    for row, d in enumerate(docs):
        for w in d:
            col = vocab.get(w)
            if col is not None:
                mat[row, col] = math.log(n / df[w])
    return _unit(mat)


def by_meaning(notes: Sequence[Note]) -> bool:
    """Whether the vectors are the features: only when every note has one, of
    one size. Otherwise the words, for all of them, so that a vector that
    arrives from the indexer between two calls does not move the groups;
    they change once, when the last one is there."""
    if not notes or any(n.vector is None for n in notes):
        return False
    return len({len(n.vector) for n in notes}) == 1  # type: ignore[arg-type]


def _features(notes: Sequence[Note], docs: Sequence[set[str]]) -> np.ndarray:
    if not by_meaning(notes):
        return _lexical(docs)
    mat = np.stack([np.asarray(n.vector, dtype=np.float32) for n in notes])
    return _unit(mat)


def _quotas(n: int, k: int) -> tuple[int, int]:
    q, r = divmod(n, k)
    return q, r


def _assign(sims: np.ndarray, q: int, r: int) -> np.ndarray:
    """Each note to a group, best pair first, under the quotas: at most ``q+1``
    in a group, and only ``r`` groups get ``q+1``."""
    # In rounds: every free note asks for its best group that has room; the
    # asks are granted best first while there is room; a note turned away
    # asks again in the next round, among the groups left.
    n, k = sims.shape
    label = np.full(n, -1, dtype=np.int64)
    size = np.zeros(k, dtype=np.int64)
    big = 0
    while True:
        free = np.flatnonzero(label < 0)
        if not free.size:
            return label
        full = (size >= q + 1) | ((size >= q) & (big >= r))
        asked = sims[free]
        asked[:, full] = -np.inf
        choice = asked.argmax(axis=1)
        best = asked[np.arange(free.size), choice]
        # the asks best first, and the place of each among the asks of its group
        order = np.lexsort((free, -best))
        points, wanted, sim = free[order], choice[order], best[order]
        by_group = np.argsort(wanted, kind="stable")
        starts = np.searchsorted(wanted[by_group], np.arange(k))
        place = np.empty(points.size, dtype=np.int64)
        place[by_group] = np.arange(points.size) - starts[wanted[by_group]]
        # up to q in every group
        room = np.maximum(q - size, 0)
        take = place < room[wanted]
        # the one more (q + 1) for at most r - big groups, the best asks first
        extra = ~take & (place == room[wanted]) & (size[wanted] + room[wanted] == q)
        if big < r and extra.any():
            ranked = np.flatnonzero(extra)[np.argsort(-sim[extra], kind="stable")]
            take[ranked[: r - big]] = True
        label[points[take]] = wanted[take]
        size += np.bincount(wanted[take], minlength=k)
        big = int((size == q + 1).sum())


def _kmeans(x: np.ndarray, k: int, seed: int, tries: int) -> np.ndarray:
    """Balanced k-means: group of each row, sizes within one of each other.
    ``tries`` starts, the one whose notes are nearest their centres kept: a
    start under quotas can settle with two subjects swapped across groups."""
    best: tuple[float, np.ndarray] | None = None
    for t in range(tries):
        label, score = _kmeans_once(x, k, seed + t)
        if best is None or score > best[0] + 1e-6:
            best = (score, label)
    assert best is not None
    return best[1]


def _kmeans_once(x: np.ndarray, k: int, seed: int) -> tuple[np.ndarray, float]:
    n = x.shape[0]
    q, r = _quotas(n, k)
    rng = np.random.default_rng(seed)
    centres = np.empty((k, x.shape[1]), dtype=np.float32)
    centres[0] = x[int(rng.integers(n))]
    nearest = np.clip(1.0 - x @ centres[0], 0, None)
    # greedy k-means++: of a few candidates drawn by distance, the one that
    # brings the notes nearest to a centre
    draws = 2 + int(math.log(k))
    for c in range(1, k):
        weight = nearest**2
        total = float(weight.sum())
        if total > 0:
            picks = rng.choice(n, size=draws, p=weight / total)
        else:
            picks = rng.integers(n, size=draws)
        after = np.minimum(nearest[None, :], np.clip(1.0 - x[picks] @ x.T, 0, None))
        best = int(np.argmin((after**2).sum(axis=1)))
        centres[c] = x[picks[best]]
        nearest = after[best]
    label = _assign(x @ centres.T, q, r)
    for _ in range(ROUNDS):
        # every group has members (the quotas give each at least q >= 1)
        centres = _unit(np.eye(k, dtype=np.float32)[label].T @ x)
        moved = _assign(x @ centres.T, q, r)
        if np.array_equal(moved, label):
            break
        label = moved
    sims = x @ centres.T
    return label, float(sims[np.arange(n), label].sum())


def _labels(
    members: Sequence[Sequence[int]],
    docs: Sequence[set[str]],
    shown: Mapping[str, str],
    above: tuple[str, ...],
) -> list[tuple[str, ...]]:
    """Three words per sibling group that set it apart from the others."""
    counts: list[Counter[str]] = []
    for rows in members:
        cnt: Counter[str] = Counter()
        for row in rows:
            cnt.update(
                w for w in docs[row] if w not in above and shown.get(w, w) not in above
            )
        counts.append(cnt)
    spread: Counter[str] = Counter()
    groups_with: Counter[str] = Counter()
    for cnt in counts:
        spread.update(cnt)
        groups_with.update(cnt.keys())
    # a word in most of the groups tells them apart from none ("meeting")
    if len(counts) >= 4:
        common = {w for w, c in groups_with.items() if c > len(counts) / 2}
        counts = [
            Counter({w: c for w, c in cnt.items() if w not in common}) for cnt in counts
        ]
    sizes = [sum(c.values()) for c in counts if c]
    avg = sum(sizes) / len(sizes) if sizes else 0.0
    out: list[tuple[str, ...]] = []
    for cnt in counts:
        total = sum(cnt.values())
        if not total:
            out.append(())
            continue
        scored = sorted(
            cnt,
            key=lambda w: (-(cnt[w] / total) * math.log(1 + avg / spread[w]), w),
        )
        out.append(tuple(shown.get(w, w) for w in scored[:LABEL_WORDS]))
    return out


def build(notes: Sequence[Note]) -> Group:
    """The groups of ``notes`` (newest first), as a tree from the whole set."""
    root = Group("", tuple(n.id for n in notes))
    if len(notes) <= FLAT:
        return root
    raw = [set(words_of(n.text)) for n in notes]
    plural = _plural_map(raw)
    docs = [{plural.get(w, w) for w in d} for d in raw]
    x = _features(notes, docs)
    _split(root, list(range(len(notes))), notes, x, docs, _shown_forms(raw, plural))
    return root


def _shown_forms(raw: Sequence[set[str]], plural: Mapping[str, str]) -> dict[str, str]:
    """For a word that stands for its plural too: the form more notes use."""
    used: Counter[str] = Counter()
    for d in raw:
        used.update(w for w in d if w in plural or w in plural.values())
    return {one: many for many, one in plural.items() if used[many] > used[one]}


def _split(
    group: Group,
    rows: list[int],
    notes: Sequence[Note],
    x: np.ndarray,
    docs: Sequence[set[str]],
    shown: Mapping[str, str],
) -> None:
    n = len(rows)
    k = min(len(LETTERS), math.ceil(n / GROUP_SIZE))
    ids = [notes[r].id for r in rows]
    # the first split is the one an assistant reads first: the most starts
    label = _kmeans(x[rows], k, _seed(ids), TRIES if not group.path else 1)
    parts = [[rows[i] for i in range(n) if label[i] == c] for c in range(k)]
    # letters: the bigger group first, then the one with the newest note
    parts.sort(key=lambda p: (-len(p), p[0]))
    words = _labels(parts, docs, shown, group.label)
    for letter, part, label_words in zip(LETTERS, parts, words, strict=False):
        child = Group(
            group.path + letter, tuple(notes[r].id for r in part), label_words
        )
        group.children.append(child)
        if len(part) > FLAT:
            _split(child, part, notes, x, docs, shown)


# ------------------------------------------------------------- read and answer


def _model() -> str | None:
    """The model whose stored vectors are features, or None for the words:
    no embedder, or one whose vectors mean nothing (the hash embedder)."""
    try:
        embedder = get_embedder(Settings.from_env())
    except ReticulaError:
        return None
    if getattr(embedder, "cheap", False):
        return None
    return str(embedder.model)


def load(conn: Conn, viewer: Account, hub_id: int) -> tuple[list[Note], int]:
    """The notes of the hub's subtree that ``search(query="", hub=...)`` lists
    (``find.scope``: what the viewer may read, current notes only), sub-hubs
    not among them, newest first: at most ``MOST`` of them, and how many there
    are. Each with its vector of the current model for its head version.

    The conditions are those of ``find.scope`` with a hub and no kind, in one
    statement: the members of the subtree (``find.subtree_sql``), of them the
    current ones that the viewer may read (``current_among``, asked about
    those ids alone, not about everything the viewer may read), not deleted,
    no debug note."""
    where = (
        "n.id IN (SELECT c.node_id FROM current_among(%(viewer)s, ARRAY("
        f"SELECT s.node_id FROM ({find.subtree_sql()}) s)) c)"
        " AND n.tombstoned_at IS NULL AND n.kind <> 'debug'"
    )
    model = _model()
    params: dict[str, Any] = {
        "viewer": viewer.id,
        "hub": hub_id,
        "person": viewer.person_id,
        "hubs": readable_hub_ids(conn, viewer),
        "model": model,
        "chars": TEXT_CHARS,
        "most": MOST + 1,
    }
    vector = (
        "(SELECT e.vec FROM embedding e WHERE e.node_id = n.id"
        " AND e.model = %(model)s AND e.version_hash = n.head_version)"
        if model
        else "NULL"
    )
    rows = conn.execute(
        "SELECT n.id, coalesce(n.title, '') || ' ' || left(n.text, %(chars)s)"
        f" AS text, {vector} AS vec FROM node n WHERE {where} AND n.kind <> 'hub'"
        " ORDER BY n.created_at DESC, n.id DESC LIMIT %(most)s",
        params,
        # the vectors as binary: parsing 5,000 of them from text costs more
        # than the grouping
        binary=True,
    ).fetchall()
    total = len(rows)
    if total > MOST:
        counted = conn.execute(
            f"SELECT count(*) AS n FROM node n WHERE {where} AND n.kind <> 'hub'",
            params,
        ).fetchone()
        total = int(counted["n"]) if counted else total
    notes = [Note(r["id"], r["text"], _array(r["vec"])) for r in rows[:MOST]]
    return notes, total


def _array(vec: Any) -> np.ndarray | None:
    """pgvector's ``Vector`` (or a plain sequence) as an array."""
    if vec is None:
        return None
    if hasattr(vec, "to_numpy"):
        return np.asarray(vec.to_numpy(), dtype=np.float32)
    return np.asarray(vec, dtype=np.float32)


def _label_text(group: Group) -> str:
    return ", ".join(group.label) if group.label else "(no common words)"


def _group_lines(group: Group, ref: str) -> list[str]:
    out = [f"{c.path}  {_label_text(c)}  ({len(c.ids)})" for c in group.children]
    first = group.children[0].path
    out.append(f'next: get(ref="{ref}", part="{first}") lists a group')
    if not group.path:
        out.append(f'the newest notes first: search(query="", hub="{ref}")')
    return out


def overview(group: Group, ref: str, total: int, notes: Sequence[Note]) -> list[str]:
    """What ``get`` on a big hub shows in place of its newest-first page. Its
    first line says what the groups go by: meaning (every note has a vector),
    or the words, and when some notes are still being indexed, that the
    groups and letters change once they are."""
    lines = []
    if total > len(group.ids):
        lines.append(f"grouped: the newest {len(group.ids):,} of the {total:,} notes")
    waiting = sum(n.vector is None for n in notes) if _model() else 0
    if by_meaning(notes):
        basis = "by meaning"
    elif waiting:
        basis = (
            f"by their words ({waiting} not indexed yet; when they are, the groups"
            " go by meaning and the letters change)"
        )
    else:
        basis = "by their words"
    lines.append(
        f"{len(group.ids)} notes in {len(group.children)} groups {basis}, each"
        " labelled with words of its notes (the letters are for your calls and"
        " hold for this listing only: name a group to the person by its words,"
        " and keep a note's id, not its letters):"
    )
    return lines + _group_lines(group, ref)


def find_part(group: Group, part: str) -> Group:
    """The group at ``part``, or BadInput that names the letters there are."""
    letters = part.strip().upper()
    if not letters or not letters.isascii() or not letters.isalpha():
        raise ReticulaError(BAD_INPUT, f"part is letters from A: {part[:20]!r}")
    if not group.children:
        raise ReticulaError(
            BAD_INPUT,
            f"this hub has {len(group.ids)} notes and is not in groups",
            hint="read it without part",
        )
    at = group
    for n, letter in enumerate(letters):
        nxt = at.find(letter)
        if nxt is None:
            have = at.letters()
            if not have:
                raise ReticulaError(
                    BAD_INPUT,
                    f"group {at.path} is not split: it lists its {len(at.ids)} notes",
                    hint=f'part="{at.path}"',
                )
            where = f"group {at.path}" if at.path else "this hub"
            raise ReticulaError(
                BAD_INPUT,
                f"no group {letters[: n + 1]}: {where} has the groups"
                f" {at.path}{have[0]} to {at.path}{have[-1]}",
            )
        at = nxt
    return at


def part_head(group: Group, siblings: int) -> str:
    """The line of the group that was asked for, as its listing had it."""
    return (
        f"group {group.path} of {siblings}: {_label_text(group)}"
        f" ({len(group.ids)} notes; keep a note's id, not its letters)"
    )


def part_lines(group: Group, ref: str, members: list[str]) -> list[str]:
    """Below the group's line: its own groups, or its notes."""
    if group.children:
        return [f"in {len(group.children)} groups:", *_group_lines(group, ref)]
    return [f"members (newest first, {len(members)}):", *members]
