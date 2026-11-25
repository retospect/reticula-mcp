"""Relevance set for the hybrid search, and a measurement of the real model.

The data set is a small memory graph (about 40 notes in six domains) and a list
of queries in five classes: paraphrases (no word in common with the target),
exact titles, keywords, nonsense (must return nothing) and off-topic questions
(measured, not asserted).

Two kinds of test live here:

* kept tests: through ``handlers.search`` with the constants as they are in
  ``src/``, for the real model (skipped when it is not available) and for the
  hash embedder (always runs);
* one measurement, run only with ``RETICULA_SEARCH_MEASURE=1`` (use ``-s``):
  per-leg scores, similarity distributions and an offline grid search of the
  fusion weights and cutoffs. It prints tables and asserts nothing.
"""

from __future__ import annotations

import itertools
import math
import os
import statistics
import time
from dataclasses import dataclass, field, replace
from typing import Any, NamedTuple

import pytest

from reticula_mcp.accounts import Account
from reticula_mcp.config import Settings
from reticula_mcp.db.pool import Conn
from reticula_mcp.search import embedder as emb
from reticula_mcp.search import find, handlers, index
from reticula_mcp.search.embedder import Embedder
from reticula_mcp.search.render import short_ref
from tests.helpers import make_atom
from tests.search_support import use_hash_embedder

# --------------------------------------------------------------------------- data


class Note(NamedTuple):
    key: str
    kind: str
    title: str | None
    text: str


class Query(NamedTuple):
    cls: str
    text: str
    expect: tuple[str, ...] = ()


NOTES: list[Note] = [
    # software operations
    Note(
        "pg-backup",
        "note",
        "Nightly database backup",
        "The production Postgres cluster is dumped every night at 02:15 UTC with"
        " pg_dump and copied to the cold storage bucket. Dumps are kept for 30"
        " days. Ben owns the restore drill, which runs on the first Monday of"
        " each month.",
    ),
    Note(
        "pg-vacuum",
        "note",
        "Autovacuum tuning for the orders table",
        "We lowered the autovacuum scale factor on the orders table to 0.02 after"
        " bloat slowed down the checkout queries. Ada watched the dead tuples for"
        " a week and the table size stopped growing.",
    ),
    Note(
        "deploy-freeze",
        "note",
        "Deploy freeze on Fridays",
        "Decision from the engineering sync: no production releases after 14:00"
        " on Fridays. Hotfixes need approval from the on-call lead. The rule"
        " applies to every service, including the internal dashboards.",
    ),
    Note(
        "oncall",
        "note",
        "On-call rotation",
        "On-call shifts last one week and hand over on Tuesday mornings. The"
        " outgoing engineer writes a short summary of open incidents in the"
        " handover note. Pager escalation goes to the team lead after fifteen"
        " minutes without an acknowledgement.",
    ),
    Note(
        "cert-renew",
        "note",
        "Certificate renewal",
        "Renew the TLS certificates with the acme client every 60 days. The cron"
        " job on the edge proxy runs the renewal and reloads nginx. If the"
        " renewal fails, the alert fires in the ops channel.",
    ),
    Note(
        "cache-evict",
        "note",
        "Cache eviction policy",
        "The session cache uses allkeys-lru with a 2 GB memory limit. More than"
        " 100 evictions per second means the limit is too low and should be"
        " raised before the next campaign.",
    ),
    Note(
        "rollback",
        "note",
        "How to roll back a release",
        "Run the rollback script with the previous image tag, wait for the health"
        " checks to turn green, then post in the releases channel. Never undo a"
        " schema migration by hand.",
    ),
    Note(
        "incident-march",
        "note",
        "Incident review: checkout outage in March",
        "On a Tuesday in March the checkout service returned errors for forty"
        " minutes. The first alert came from the payment provider, not from our"
        " own monitoring, which is the main finding of this review. The cause was"
        " an exhausted connection pool: a new report query held connections open"
        " while the nightly database backup was still running, and the pool had"
        " no spare capacity. Ada spotted the pattern in the slow query log and"
        " killed the report. Ben scaled the pool from 40 to 80 connections and the"
        " error rate fell within five minutes. Follow-ups: alert on pool"
        " saturation at 80 percent, move the report to the read replica, and keep"
        " the deploy freeze on Fridays because the previous release touched the"
        " same pool settings. The on-call summary was posted in the incident"
        " channel within the hour and customers received a short apology by"
        " e-mail. No orders were lost, but about 300 carts were abandoned during"
        " the outage.",
    ),
    # cooking
    Note(
        "sourdough",
        "note",
        "Sourdough bread",
        "Feed the starter twelve hours before baking. Mix 500 g flour, 350 g"
        " water and 10 g salt, fold the dough four times during the first rise,"
        " and bake in a covered pot at 240 degrees for 25 minutes.",
    ),
    Note(
        "pizza",
        "note",
        "Neapolitan pizza dough",
        "Use 00 flour, 65 percent hydration and a long cold ferment of 48 hours in"
        " the fridge. Unlike sourdough bread it takes a pinch of fresh yeast."
        " Shape by hand and bake as hot as the oven goes.",
    ),
    Note(
        "lentil-soup",
        "note",
        "Red lentil soup",
        "Saute onion, carrot and cumin, add rinsed red lentils and stock, simmer"
        " for twenty minutes and finish with lemon juice. It freezes well.",
    ),
    Note(
        "knife",
        "note",
        None,
        "Sharpen the kitchen knives on the whetstone every two months. Soak the"
        " stone for ten minutes first and hold a fifteen degree angle.",
    ),
    Note(
        "anna-allergy",
        "note",
        "Anna's food allergies",
        "Anna is allergic to walnuts and pistachios. When cooking for the team"
        " dinner, avoid every nut and check the labels on the pesto.",
    ),
    Note(
        "coffee",
        "note",
        "Ben's coffee preference",
        "Ben takes his coffee as a flat white with oat milk and no sugar. The"
        " office machine needs descaling every month.",
    ),
    # travel
    Note(
        "lisbon",
        "note",
        "Lisbon trip in May",
        "We booked flights for the second week of May and an apartment in Alfama"
        " for six nights. Tram 28 is crowded, so go early. Pastel de nata at the"
        " bakery near the cathedral.",
    ),
    Note(
        "jp-rail",
        "note",
        "Japan rail pass",
        "The seven day rail pass pays off only if we ride the bullet train between"
        " Tokyo and Kyoto and back at least once more. Otherwise buy single"
        " tickets.",
    ),
    Note(
        "jp-itinerary",
        "note",
        "Japan itinerary draft",
        "Draft plan for two weeks in autumn. Days one to four in Tokyo: the fish"
        " market in the morning, the museum district and a day trip to the"
        " mountains. Day five by bullet train to Kyoto, where we stay in a small"
        " guesthouse near the river. We should decide about the Japan rail pass"
        " before booking, since reservations for the fast trains open a month"
        " ahead. Days six to nine in Kyoto and Nara: temples early in the morning"
        " to avoid the crowds, a tea ceremony booked for Thursday, deer in the"
        " park. Days ten to twelve in Osaka for street food and the castle. The"
        " last two days are kept free for weather or tired legs. Luggage is"
        " forwarded between hotels by courier, so only a carry-on travels by"
        " train. Cash is still needed in small restaurants.",
    ),
    Note(
        "packing",
        "note",
        "Carry-on packing list",
        "Passport, charger, merino shirts, a small first-aid kit and a refillable"
        " water bottle. Liquids stay under 100 ml in a clear bag.",
    ),
    Note(
        "visa",
        "note",
        "Visa rules for the Canada trip",
        "Citizens of Switzerland need an eTA before flying to Canada. It costs"
        " seven dollars and is approved within minutes. Apply at least three days"
        " before departure.",
    ),
    Note(
        "night-train",
        "note",
        None,
        "The night train from Zurich to Vienna leaves at 21:40 and arrives at"
        " 07:30. Book a couchette early because the cheap berths sell out weeks"
        " ahead.",
    ),
    # health
    Note(
        "sleep",
        "note",
        "Sleep schedule",
        "Aim for bed at 23:00 and wake at 07:00. No screens for the last hour."
        " Caffeine only before noon.",
    ),
    Note(
        "run-plan",
        "note",
        "Half marathon training plan",
        "Twelve week plan with three runs per week: an easy run, intervals on"
        " Wednesday and a long run on Sunday that grows by one kilometre each"
        " week. Rest day before the long run.",
    ),
    Note(
        "knee",
        "note",
        "Knee physiotherapy exercises",
        "Do the step-downs and wall sits twice a day, ten repetitions each. Pause"
        " the half marathon training plan until the pain is gone. The"
        " physiotherapist wants to see me again in six weeks.",
    ),
    Note(
        "vitd",
        "note",
        "Vitamin D",
        "The blood test in January showed low vitamin D. The doctor recommended"
        " 2000 units daily through winter and a new test in April.",
    ),
    # finance
    Note(
        "tax",
        "note",
        "Tax return deadline",
        "The personal tax return is due on 31 March. Collect the salary"
        " certificate, the pillar 3a statement and the donation receipts before"
        " then.",
    ),
    Note(
        "budget",
        "note",
        "Monthly budget",
        "Rent takes about 35 percent of income. Savings of 20 percent move to the"
        " index fund on payday. Groceries are capped at 600 per month.",
    ),
    Note(
        "mortgage",
        "note",
        "Mortgage rate decision",
        "We chose the five year fixed rate at 2.1 percent over the variable rate."
        " The bank wants proof of income and the property valuation before"
        " signing.",
    ),
    Note(
        "etf",
        "note",
        "Index fund choice",
        "A broad world index ETF with low fees. Rebalance once per year and do"
        " not check the price every day.",
    ),
    Note(
        "invoice",
        "note",
        "Invoice template for freelance work",
        "Invoices carry a net 30 payment term, the VAT number and a reference"
        " number per project. Send them on the last working day of the month.",
    ),
    Note(
        "expense-policy",
        "note",
        "Household expense policy",
        "How we handle shared money in the household. Every month each partner"
        " pays an equal amount into the joint account, which covers rent,"
        " utilities, groceries and insurance. Personal spending stays on personal"
        " cards. Any single purchase above 300 needs a short chat first. Holidays"
        " are paid from a separate pot that receives a fixed transfer on payday."
        " When the mortgage rate decision comes up again in five years we will"
        " revisit the split, because the fixed period ends then. Receipts for"
        " anything deductible go into the tax folder at once, so March is not a"
        " scramble. Subscriptions are reviewed every January: cancel anything"
        " unused for three months. Large gifts and family loans are written down"
        " with the date and the agreed repayment, so nobody has to remember the"
        " details later. The budget sheet is updated on the first Sunday of every"
        " month by whoever has more time.",
    ),
    # research
    Note(
        "p-attention",
        "paper",
        "Attention Is All You Need",
        "Stub for the 2017 transformer paper. It replaces recurrence with"
        " self-attention over the whole sequence. To read: sections 3 and 4.",
    ),
    Note(
        "p-bert",
        "paper",
        "BERT: Pre-training of Deep Bidirectional Transformers",
        "Stub for the 2018 paper on masked language model pretraining. Useful as a"
        " baseline for the sentence encoders in the retrieval project.",
    ),
    Note(
        "p-rag",
        "paper",
        "Retrieval-Augmented Generation for Knowledge-Intensive Tasks",
        "Stub for the 2020 paper that joins a dense retriever to a"
        " sequence-to-sequence generator for open-domain question answering."
        " Cited in the project proposal.",
    ),
    Note(
        "p-crispr",
        "paper",
        "A Programmable Dual-RNA-Guided DNA Endonuclease in Adaptive Bacterial"
        " Immunity",
        "Stub for the 2012 paper that showed how a guide RNA directs a bacterial"
        " enzyme to cut DNA at a chosen site. Background for the gene editing"
        " seminar.",
    ),
    Note(
        "lit-method",
        "note",
        "Literature review method",
        "Search three databases, record the query and the date, screen titles"
        " first, then abstracts, then full text. Keep a log of why papers were"
        " excluded.",
    ),
    Note(
        "stats-power",
        "note",
        "Sample size for the pilot study",
        "A power analysis for a two group comparison with effect size 0.5 and 80"
        " percent power needs about 64 participants per group. We settled on 70"
        " to allow for dropouts.",
    ),
    Note(
        "reading-group",
        "note",
        "Reading group meeting 12 June",
        "Outcome: the next session discusses the retrieval paper and Maria will"
        " present it. Everyone should skim the literature review method note"
        " first. The meeting moves to Thursdays at 16:00.",
    ),
    Note(
        "pilot-protocol",
        "note",
        "Pilot study protocol",
        "Protocol for the pilot with 70 volunteers per arm. Following the"
        " literature review method, we screened 212 abstracts and kept 18 papers"
        " for the background section. Participants are recruited through the"
        " university mailing list and paid a flat fee per session. Each session"
        " lasts forty minutes: consent form, a short questionnaire, two timed"
        " tasks on the laptop and a debrief. Data are stored on the encrypted"
        " lab drive under a participant number, never under a name. The analysis"
        " plan is fixed before the first session: a mixed model with the task as"
        " a fixed effect and the participant as a random effect, no peeking at"
        " the outcome before all sessions are done. Deviations from the plan go"
        " into a dated log. Ethics approval was granted in spring and expires in"
        " twelve months, so the pilot must finish well before.",
    ),
    # undated odds and ends without a title
    Note(
        "wifi",
        "note",
        None,
        "The guest network is called Annex and its password is on the card behind"
        " the router. The office printer is on the second floor.",
    ),
    Note(
        "standup",
        "note",
        None,
        "Standup outcome: Ada takes the flaky login test, Ben reviews the"
        " invoice export, and nobody touches the billing service until Monday.",
    ),
    # hubs
    Note(
        "hub-ops",
        "hub",
        "Operations runbook",
        "Everything about running the production systems: backups, releases,"
        " certificates and incident reviews.",
    ),
    Note(
        "hub-kitchen",
        "hub",
        "Kitchen",
        "Recipes, techniques and food preferences of the household.",
    ),
    Note(
        "hub-research",
        "hub",
        "Research reading",
        "Papers and working notes for the retrieval project.",
    ),
]

PARAPHRASE = "paraphrase"
EXACT = "exact title"
KEYWORD = "keyword"
NONSENSE = "nonsense"
OFFTOPIC = "off-topic"
ANSWERABLE = (PARAPHRASE, EXACT, KEYWORD)

QUERIES: list[Query] = [
    # paraphrases: no word (after stemming) in common with title or text
    Query(
        PARAPHRASE,
        "who rehearses getting our data back after a disaster",
        ("pg-backup",),
    ),
    Query(
        PARAPHRASE,
        "why can't we ship new versions late on the last day of the working week",
        ("deploy-freeze",),
    ),
    Query(PARAPHRASE, "who gets woken up when something breaks", ("oncall",)),
    Query(
        PARAPHRASE,
        "how do I make a crusty loaf from a wild yeast culture",
        ("sourdough",),
    ),
    Query(
        PARAPHRASE,
        "a warming vegetarian dish for a cold evening using pulses",
        ("lentil-soup",),
    ),
    Query(
        PARAPHRASE,
        "who can't eat hazelnuts or cashews at our group meal",
        ("anna-allergy",),
    ),
    Query(
        PARAPHRASE,
        "where are we sleeping when we visit Portugal's capital in spring",
        ("lisbon",),
    ),
    Query(
        PARAPHRASE,
        "is a discounted travel card for the Shinkansen worth it",
        ("jp-rail",),
    ),
    Query(
        PARAPHRASE,
        "do Swiss passport holders require advance entry clearance for Toronto",
        ("visa",),
    ),
    Query(
        PARAPHRASE,
        "when should I stop looking at my phone in the evening",
        ("sleep",),
    ),
    Query(
        PARAPHRASE,
        "how should I get ready for a 21 km race this autumn",
        ("run-plan",),
    ),
    Query(
        PARAPHRASE,
        "which workouts did my therapist give me for my sore joint",
        ("knee",),
    ),
    Query(
        PARAPHRASE,
        "when do I have to hand in my yearly income declaration to the authorities",
        ("tax",),
    ),
    Query(
        PARAPHRASE,
        "why did we pick a locked-in interest for our home loan",
        ("mortgage",),
    ),
    Query(
        PARAPHRASE,
        "how do we decide which studies to leave out of the survey of existing work",
        ("lit-method",),
    ),
    Query(
        PARAPHRASE,
        "language models that look things up in a corpus before responding",
        ("p-rag",),
    ),
    Query(
        PARAPHRASE,
        "how many volunteers should we recruit for the small preliminary experiment",
        ("stats-power",),
    ),
    Query(
        PARAPHRASE,
        "why did visitors see a scary padlock warning in their browser",
        ("cert-renew",),
    ),
    # exact titles (several occur in the body of other notes)
    Query(EXACT, "Nightly database backup", ("pg-backup",)),
    Query(EXACT, "Deploy freeze on Fridays", ("deploy-freeze",)),
    Query(EXACT, "Sourdough bread", ("sourdough",)),
    Query(EXACT, "Half marathon training plan", ("run-plan",)),
    Query(EXACT, "Mortgage rate decision", ("mortgage",)),
    Query(EXACT, "Literature review method", ("lit-method",)),
    Query(EXACT, "Japan rail pass", ("jp-rail",)),
    # keywords from the body of the target
    Query(KEYWORD, "couchette", ("night-train",)),
    Query(KEYWORD, "whetstone", ("knife",)),
    Query(KEYWORD, "walnuts", ("anna-allergy",)),
    Query(KEYWORD, "bloat", ("pg-vacuum",)),
    Query(KEYWORD, "descaling", ("coffee",)),
    Query(KEYWORD, "Alfama tram", ("lisbon",)),
    # nonsense: must return nothing
    Query(NONSENSE, "asdkjh qwlekj zxmnv"),
    Query(NONSENSE, "blorptang fizzwumble quonk"),
    Query(NONSENSE, "xqzv jjkk pplm"),
    Query(NONSENSE, "k3j4h5 9a8s7d zz00"),
    Query(NONSENSE, "mnbvcx lkjhgf poiuyt"),
    Query(NONSENSE, "qwrtp 8841 zzxkq"),
    # off-topic: sensible questions on subjects no note covers (measured only)
    Query(OFFTOPIC, "What is the capital of Mongolia?"),
    Query(OFFTOPIC, "How do I patch a bicycle inner tube?"),
    Query(OFFTOPIC, "Explain how photosynthesis works"),
    Query(OFFTOPIC, "Who won the football world cup in 2010?"),
]

NOTE_BY_KEY = {n.key: n for n in NOTES}


def of_class(cls: str) -> list[Query]:
    return [q for q in QUERIES if q.cls == cls]


# ------------------------------------------------------------------------- world


class World(NamedTuple):
    account: Account
    key_of: dict[int, str]  # node id -> key
    id_of: dict[str, int]
    key_of_short: dict[str, str]  # short ref shown by search -> key


def build_world(conn: Conn, account: Account, embedder: Embedder) -> World:
    key_of: dict[int, str] = {}
    id_of: dict[str, int] = {}
    key_of_short: dict[str, str] = {}
    for note in NOTES:
        row = make_atom(conn, account, note.text, kind=note.kind, title=note.title)
        key_of[row["id"]] = note.key
        id_of[note.key] = row["id"]
        key_of_short[short_ref(row["address"])] = note.key
    index.reindex(conn, embedder)
    return World(account, key_of, id_of, key_of_short)


def load_embedder(monkeypatch: pytest.MonkeyPatch, name: str) -> tuple[Embedder, float]:
    """The embedder ``name`` ('hash' or 'fastembed') as the search code builds it,
    and the seconds the first embed call took (the model load for fastembed).
    Skips the test when the real model cannot be loaded."""
    if name == "hash":
        use_hash_embedder(monkeypatch)
        return emb.get_embedder(Settings.from_env()), 0.0
    started = time.perf_counter()
    pytest.importorskip("fastembed")  # the heavy import counts as load time
    monkeypatch.setenv("RETICULA_EMBEDDER", "fastembed")
    monkeypatch.delenv("RETICULA_EMBED_MODEL", raising=False)
    emb._build.cache_clear()
    try:
        embedder = emb.get_embedder(Settings.from_env())
        embedder.embed(["probe"])
    except Exception as exc:  # the model download needs the network
        emb._build.cache_clear()
        pytest.skip(f"real embedding model unavailable: {exc}")
    return embedder, time.perf_counter() - started


@pytest.fixture
def owner(make_account):
    return make_account(handle="ada")


@pytest.fixture(autouse=True)
def _reset_embedder_cache():
    yield
    emb._build.cache_clear()


def search_keys(conn: Conn, world: World, query: str) -> list[str] | None:
    """Keys in the order ``handlers.search`` lists them; None for 'no results'."""
    out = handlers.search(conn, world.account, query=query, limit=10)
    lines = [ln for ln in out.splitlines() if ln.startswith("- ")]
    if not lines:
        assert ": no results." in out, out
        return None
    return [world.key_of_short[ln[2:10]] for ln in lines]


def lexemes(conn: Conn, text: str) -> set[str]:
    row = conn.execute(
        "SELECT tsvector_to_array(to_tsvector('english', %s)) AS a", (text,)
    ).fetchone()
    return set(row["a"])


def note_text(note: Note) -> str:
    return f"{note.title or ''} {note.text}"


# ------------------------------------------------------------ data set sanity


def test_data_set_shape():
    keys = [n.key for n in NOTES]
    assert len(keys) == len(set(keys))
    assert 38 <= len(NOTES) <= 50
    assert sum(1 for n in NOTES if len(n.text) >= 800) >= 3
    assert sum(1 for n in NOTES if n.title is None) >= 3
    assert {n.kind for n in NOTES} == {"note", "paper", "hub"}
    assert len(QUERIES) >= 22
    assert len(of_class(PARAPHRASE)) >= 9
    assert len(of_class(EXACT)) >= 5
    assert len(of_class(KEYWORD)) >= 4
    assert len(of_class(NONSENSE)) >= 5
    assert len(of_class(OFFTOPIC)) == 4
    for q in QUERIES:
        assert all(k in NOTE_BY_KEY for k in q.expect), q
        assert bool(q.expect) == (q.cls in ANSWERABLE), q
    for q in of_class(EXACT):
        assert [NOTE_BY_KEY[k].title for k in q.expect] == [q.text], q
    for q in of_class(EXACT):
        # a real test: the title's words also occur in the text of another note
        words = [w.lower() for w in q.text.split()]
        others = [n for n in NOTES if n.key not in q.expect]
        assert any(all(w in n.text.lower() for w in words) for n in others), q.text


def test_paraphrases_share_no_word_with_their_target(conn):
    problems = []
    for q in of_class(PARAPHRASE):
        wanted = lexemes(conn, q.text)
        assert wanted, q.text
        for key in q.expect:
            shared = wanted & lexemes(conn, note_text(NOTE_BY_KEY[key]))
            if shared:
                problems.append(f"{q.text!r} shares {sorted(shared)} with {key}")
    assert not problems, "\n".join(problems)


def test_nonsense_queries_use_no_word_of_any_note(conn):
    every_word = set()
    for n in NOTES:
        every_word |= lexemes(conn, note_text(n))
    for q in of_class(NONSENSE):
        assert not (lexemes(conn, q.text) & every_word), q.text


# ------------------------------------------------------------------ kept tests


def failures_for(conn: Conn, world: World, cls: str, ok: Any) -> list[str]:
    """Run every query of ``cls`` end to end; ``ok(query, keys)`` says whether the
    answer is right. Returns one line per wrong answer."""
    bad = []
    for q in of_class(cls):
        keys = search_keys(conn, world, q.text)
        if not ok(q, keys):
            shown = "no results" if keys is None else ", ".join(keys[:4])
            bad.append(f"{q.text!r}: expected {list(q.expect)}, got {shown}")
    return bad


def in_top(n: int) -> Any:
    return lambda q, keys: keys is not None and any(k in keys[:n] for k in q.expect)


def is_first(q: Query, keys: list[str] | None) -> bool:
    return keys is not None and keys[0] in q.expect


def is_empty(q: Query, keys: list[str] | None) -> bool:
    return keys is None


# Paraphrases the real model does not bring into the first three with the
# constants as measured: its similarity to the right note is below the cutoff
# that keeps nonsense out (pg-backup 0.56, lit-method 0.64, cert-renew 0.51), or
# a wrong note is closer (oncall before deploy-freeze). The test below fails
# when a fifth is lost, and when one of these is found (then shorten the list).
PARAPHRASE_MISSES = 4


@pytest.fixture
def real_world(conn, owner, monkeypatch) -> World:
    embedder, _ = load_embedder(monkeypatch, "fastembed")
    return build_world(conn, owner, embedder)


@pytest.fixture
def hash_world(conn, owner, monkeypatch) -> World:
    embedder, _ = load_embedder(monkeypatch, "hash")
    return build_world(conn, owner, embedder)


def test_real_model_paraphrase_target_in_top_three(conn, real_world):
    bad = failures_for(conn, real_world, PARAPHRASE, in_top(3))
    assert len(bad) == PARAPHRASE_MISSES, "\n".join(bad)


def test_real_model_exact_title_ranks_first(conn, real_world):
    bad = failures_for(conn, real_world, EXACT, is_first)
    assert not bad, "\n".join(bad)


def test_real_model_keyword_target_in_top_three(conn, real_world):
    bad = failures_for(conn, real_world, KEYWORD, in_top(3))
    assert not bad, "\n".join(bad)


def test_real_model_nonsense_returns_nothing(conn, real_world):
    bad = failures_for(conn, real_world, NONSENSE, is_empty)
    assert not bad, "\n".join(bad)


def test_hash_exact_title_ranks_first(conn, hash_world):
    bad = failures_for(conn, hash_world, EXACT, is_first)
    assert not bad, "\n".join(bad)


def test_hash_keyword_target_in_top_three(conn, hash_world):
    bad = failures_for(conn, hash_world, KEYWORD, in_top(3))
    assert not bad, "\n".join(bad)


def test_hash_nonsense_returns_nothing(conn, hash_world):
    bad = failures_for(conn, hash_world, NONSENSE, is_empty)
    assert not bad, "\n".join(bad)


# ----------------------------------------------------------------- measurement

WEIGHTS = (0.5, 1.0, 1.5, 2.0, 3.0)
TRIGRAM_GRID = (0.25, 0.3, 0.35, 0.4, 0.5)
REAL_DISTANCES = tuple(round(0.20 + 0.025 * i, 3) for i in range(17))
HASH_DISTANCES = tuple(round(0.50 + 0.05 * i, 2) for i in range(10))
# 0 = any note with one of the words, 1 = every word (no second try at all)
LOOSE_SHARES = (0.0, 0.34, 0.5, 0.67, 1.0)
RESULT_LIMIT = 10


@dataclass
class Legs:
    """Full per-leg lists for one query, best first, nothing cut yet."""

    query: Query
    expect_ids: set[int]
    lexical: list[tuple[int, float]]  # as find._lexical returns it today
    # the same leg for each value of find.LOOSE_MIN_SHARE in LOOSE_SHARES
    lexical_by_share: dict[float, list[tuple[int, float]]]
    title: list[tuple[int, float]]  # (id, trigram similarity), all titled notes
    semantic: list[tuple[int, float]]  # (id, cosine distance), all notes


def collect(
    conn: Conn,
    world: World,
    embedder: Embedder,
    monkeypatch: pytest.MonkeyPatch,
) -> list[Legs]:
    monkeypatch.setattr(embedder, "max_distance", 2.0)
    monkeypatch.setattr(find, "TRIGRAM_MIN", 0.0)
    where, params = find._scope(
        world.account, hub_node_id=None, kind=None, include_superseded=False
    )
    pool = 1000
    current_share = find.LOOSE_MIN_SHARE
    out = []
    for q in QUERIES:
        sem = find._semantic(conn, embedder, q.text, where, params, pool)
        by_share = {}
        for share in sorted({*LOOSE_SHARES, current_share}):
            monkeypatch.setattr(find, "LOOSE_MIN_SHARE", share)
            by_share[share] = find._lexical(conn, q.text, where, params, pool)
        monkeypatch.setattr(find, "LOOSE_MIN_SHARE", current_share)
        out.append(
            Legs(
                q,
                {world.id_of[k] for k in q.expect},
                by_share[current_share],
                by_share,
                find._trigram(conn, q.text, where, params, pool),
                [(i, 1.0 - sim) for i, sim in sem],
            )
        )
    return out


def percentile(values: list[float], p: float) -> float:
    if not values:
        return math.nan
    s = sorted(values)
    pos = (len(s) - 1) * p / 100.0
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def dist_row(label: str, values: list[float]) -> str:
    cells = " ".join(f"{percentile(values, p):6.3f}" for p in (0, 10, 50, 90, 100))
    return f"{label:<44}{len(values):>3} {cells}"


def fuse(parts: list[tuple[float, list[int]]]) -> list[int]:
    """Same arithmetic as ``rank_fusion.reciprocal_rank_fusion``, with weights."""
    scores: dict[int, float] = {}
    for weight, ids in parts:
        for rank, node_id in enumerate(ids):
            scores[node_id] = scores.get(node_id, 0.0) + weight / (
                find.RRF_K + rank + 1
            )
    return sorted(scores, key=lambda i: (-scores[i], -i))


class Setting(NamedTuple):
    w_lexical: float
    w_title: float
    w_semantic: float
    max_distance: float
    trigram_min: float


@dataclass
class Metrics:
    setting: Setting
    mrr: float = 0.0
    hit1: float = 0.0
    hit3: float = 0.0
    hit1_by_class: dict[str, float] = field(default_factory=dict)
    nonsense_nonempty: int = 0
    offtopic_mean: float = 0.0

    def simplicity(self, default: Setting) -> tuple[int, int, float]:
        s = self.setting
        odd_weights = sum(1 for w in (s.w_lexical, s.w_title, s.w_semantic) if w != 1.0)
        return (
            odd_weights,
            int(s.trigram_min != default.trigram_min),
            abs(s.max_distance - default.max_distance),
        )


def evaluate(data: list[Legs], setting: Setting) -> Metrics:
    cut_title = {
        id(d): [i for i, s in d.title if s >= setting.trigram_min] for d in data
    }
    cut_sem = {
        id(d): [i for i, dist in d.semantic if dist <= setting.max_distance]
        for d in data
    }
    return _evaluate(data, setting, cut_title, cut_sem)


def _evaluate(
    data: list[Legs],
    setting: Setting,
    cut_title: dict[int, list[int]],
    cut_sem: dict[int, list[int]],
) -> Metrics:
    m = Metrics(setting)
    recips: list[float] = []
    first = third = 0
    per_class: dict[str, list[int]] = {c: [] for c in ANSWERABLE}
    offtopic_counts: list[int] = []
    for d in data:
        ranked = fuse(
            [
                (setting.w_lexical, [i for i, _ in d.lexical]),
                (setting.w_title, cut_title[id(d)]),
                (setting.w_semantic, cut_sem[id(d)]),
            ]
        )[:RESULT_LIMIT]
        cls = d.query.cls
        if cls in ANSWERABLE:
            rank = next((r for r, i in enumerate(ranked) if i in d.expect_ids), None)
            recips.append(0.0 if rank is None else 1.0 / (rank + 1))
            is1 = int(rank == 0)
            first += is1
            third += int(rank is not None and rank < 3)
            per_class[cls].append(is1)
        elif cls == NONSENSE:
            m.nonsense_nonempty += int(bool(ranked))
        else:
            offtopic_counts.append(len(ranked))
    n = len(recips)
    m.mrr = sum(recips) / n
    m.hit1 = first / n
    m.hit3 = third / n
    m.hit1_by_class = {c: sum(v) / len(v) for c, v in per_class.items()}
    m.offtopic_mean = statistics.fmean(offtopic_counts)
    return m


def grid_search(data: list[Legs], distances: tuple[float, ...]) -> list[Metrics]:
    results: list[Metrics] = []
    for tmin, dmax in itertools.product(TRIGRAM_GRID, distances):
        cut_title = {id(d): [i for i, s in d.title if s >= tmin] for d in data}
        cut_sem = {id(d): [i for i, dist in d.semantic if dist <= dmax] for d in data}
        for wl, wt, ws in itertools.product(WEIGHTS, repeat=3):
            results.append(
                _evaluate(data, Setting(wl, wt, ws, dmax, tmin), cut_title, cut_sem)
            )
    return results


def metric_line(m: Metrics) -> str:
    s = m.setting
    by = m.hit1_by_class
    return (
        f"w=({s.w_lexical:g},{s.w_title:g},{s.w_semantic:g}) "
        f"maxdist={s.max_distance:<5g} trigram={s.trigram_min:<4g} | "
        f"MRR {m.mrr:.3f} hit@1 {m.hit1:.3f} hit@3 {m.hit3:.3f} | "
        f"h@1 para {by[PARAPHRASE]:.2f} exact {by[EXACT]:.2f} "
        f"kw {by[KEYWORD]:.2f} | nonsense>0: {m.nonsense_nonempty} "
        f"offtopic mean {m.offtopic_mean:.1f}"
    )


def fmt_top(
    items: list[tuple[int, float]], world: World, n: int = 5, digits: int = 3
) -> str:
    if not items:
        return "-"
    return " ".join(f"{world.key_of[i]}({s:.{digits}f})" for i, s in items[:n])


def print_per_query(
    conn: Conn, world: World, data: list[Legs], default: Setting
) -> dict[str, tuple[float, float]]:
    """Prints the per-query lines; returns, per query text, the semantic
    similarity of the expected note and of the best other note."""
    sims: dict[str, tuple[float, float]] = {}
    print("\n== 1. per query (legs: lexical score, title similarity, cosine sim)")
    for d in data:
        q = d.query
        sem_sim = [(i, 1.0 - dist) for i, dist in d.semantic]
        want = max((s for i, s in sem_sim if i in d.expect_ids), default=math.nan)
        other = max((s for i, s in sem_sim if i not in d.expect_ids), default=math.nan)
        sims[q.text] = (want, other)
        title_cut = [(i, s) for i, s in d.title if s >= default.trigram_min]
        sem_cut = [(i, s) for i, s in sem_sim if s >= 1.0 - default.max_distance]
        fused = fuse(
            [
                (1.0, [i for i, _ in d.lexical]),
                (1.0, [i for i, _ in title_cut]),
                (1.0, [i for i, _ in sem_cut]),
            ]
        )[:3]
        print(
            f"\n[{q.cls}] {q.text!r}  expect={','.join(q.expect) or '-'}\n"
            f"  lexical : {fmt_top(d.lexical, world)}  (n={len(d.lexical)})\n"
            f"  title   : {fmt_top(title_cut, world)}  (n={len(title_cut)})\n"
            f"  semantic: {fmt_top(sem_sim[:5], world)}  "
            f"(n at cutoff={len(sem_cut)})\n"
            f"  sim expected={want:.3f}  best other={other:.3f}  "
            f"fused now: {', '.join(world.key_of[i] for i in fused) or '-'}"
        )
    return sims


def separation(
    name: str, wanted: list[float], noise: list[float], unit: str | None = None
) -> str:
    """Says whether one cutoff on ``name`` keeps every ``wanted`` value and drops
    every ``noise`` value; else the cutoff with the fewest errors."""
    hi_n, lo_t = max(noise), min(wanted)
    head = (
        f"\nhighest nonsense {hi_n:.3f} | lowest paraphrase target {lo_t:.3f} | "
        f"gap {lo_t - hi_n:+.3f}"
    )
    if lo_t > hi_n:
        extra = f", i.e. {unit} in [{1 - lo_t:.3f}, {1 - hi_n:.3f})" if unit else ""
        return (
            f"{head}\nONE absolute cutoff on {name} separates nonsense from every"
            f" paraphrase target: any value in ({hi_n:.3f}, {lo_t:.3f}]{extra}."
            f" Widest gap {lo_t - hi_n:.3f}."
        )
    best: tuple[int, float] | None = None
    for t in sorted({*wanted, *noise}):
        errors = sum(v >= t for v in noise) + sum(v < t for v in wanted)
        if best is None or errors < best[0]:
            best = (errors, t)
    assert best is not None
    extra = f" ({unit} {1 - best[1]:.3f})" if unit else ""
    return (
        f"{head}\nNO single cutoff on {name} separates nonsense from every"
        f" paraphrase target. Fewest errors ({best[0]} of "
        f"{len(wanted) + len(noise)}) at {name} >= {best[1]:.3f}{extra}."
    )


def zscores(d: Legs) -> tuple[float, float]:
    """(z of the expected note, z of the best other note) among all similarities
    of one query; z = (similarity - mean) / standard deviation over all notes."""
    sims = [1.0 - dist for _, dist in d.semantic]
    mean, sd = statistics.fmean(sims), statistics.pstdev(sims)
    want = max(
        (1.0 - dist for i, dist in d.semantic if i in d.expect_ids), default=math.nan
    )
    other = max((1.0 - dist for i, dist in d.semantic if i not in d.expect_ids))
    return (want - mean) / sd, (other - mean) / sd


def print_relative(data: list[Legs]) -> None:
    print("\n== 2b. relative score: z of a note among all notes for that query")
    print("(z = (similarity - mean over notes) / standard deviation over notes)")
    print(f"{'':<44}{'n':>3} {'min':>6} {'p10':>6} {'med':>6} {'p90':>6} {'max':>6}")
    rows: dict[str, list[tuple[float, float]]] = {}
    for d in data:
        rows.setdefault(d.query.cls, []).append(zscores(d))
    para = [w for w, _ in rows[PARAPHRASE]]
    nonsense = [o for _, o in rows[NONSENSE]]
    print(dist_row("z paraphrase target", para))
    print(dist_row("z exact title target", [w for w, _ in rows[EXACT]]))
    print(dist_row("z keyword target", [w for w, _ in rows[KEYWORD]]))
    print(dist_row("z best WRONG note, paraphrase", [o for _, o in rows[PARAPHRASE]]))
    print(dist_row("z best note, nonsense", nonsense))
    print(dist_row("z best note, off-topic", [o for _, o in rows[OFFTOPIC]]))
    print(separation("z", para, nonsense))


def print_distributions(data: list[Legs], sims: dict[str, tuple[float, float]]) -> None:
    print("\n== 2. semantic cosine similarity (1 - distance)")
    print(f"{'':<44}{'n':>3} {'min':>6} {'p10':>6} {'med':>6} {'p90':>6} {'max':>6}")
    by_class = {
        c: [d for d in data if d.query.cls == c]
        for c in (*ANSWERABLE, NONSENSE, OFFTOPIC)
    }
    target = {c: [sims[d.query.text][0] for d in by_class[c]] for c in ANSWERABLE}
    wrong = {c: [sims[d.query.text][1] for d in by_class[c]] for c in ANSWERABLE}
    print(dist_row("paraphrase -> target", target[PARAPHRASE]))
    print(dist_row("exact title -> target", target[EXACT]))
    print(dist_row("keyword -> target", target[KEYWORD]))
    print(dist_row("best WRONG note, paraphrase queries", wrong[PARAPHRASE]))
    print(dist_row("best WRONG note, exact title queries", wrong[EXACT]))
    print(dist_row("best WRONG note, keyword queries", wrong[KEYWORD]))
    print(
        dist_row(
            "best WRONG note, all answerable",
            [v for c in ANSWERABLE for v in wrong[c]],
        )
    )
    nonsense = [sims[d.query.text][1] for d in by_class[NONSENSE]]
    offtopic = [sims[d.query.text][1] for d in by_class[OFFTOPIC]]
    print(dist_row("best note, nonsense queries", nonsense))
    print(dist_row("best note, off-topic queries", offtopic))
    print(separation("similarity", target[PARAPHRASE], nonsense, "max_distance"))
    lo_w = min(target[PARAPHRASE])
    hi_w = max(offtopic)
    print(
        f"highest off-topic {hi_w:.3f} vs lowest paraphrase target {lo_w:.3f}:"
        f" gap {lo_w - hi_w:+.3f}"
    )
    wrong_p = max(wrong[PARAPHRASE])
    print(
        f"paraphrase targets below the best wrong note of their own query:"
        f" {sum(t < w for t, w in zip(target[PARAPHRASE], wrong[PARAPHRASE], strict=True))}"
        f" of {len(target[PARAPHRASE])} (highest wrong {wrong_p:.3f})"
    )


def print_unwanted_legs(
    conn: Conn, world: World, data: list[Legs], tmin: float
) -> None:
    print("\n== 3. lexical and title legs on nonsense and off-topic queries")
    seen = False
    for d in data:
        if d.query.cls not in (NONSENSE, OFFTOPIC):
            continue
        q = d.query
        if d.lexical:
            seen = True
            top = d.lexical[0][0]
            note = NOTE_BY_KEY[world.key_of[top]]
            shared = sorted(lexemes(conn, q.text) & lexemes(conn, note_text(note)))
            print(
                f"lexical [{q.cls}] {q.text!r}: {len(d.lexical)} results, top "
                f"{fmt_top(d.lexical, world, 3)}; query word(s) in the note: {shared}"
            )
        hits = [(i, s) for i, s in d.title if s >= tmin]
        if hits:
            seen = True
            i, s = hits[0]
            print(
                f"title   [{q.cls}] {q.text!r}: {len(hits)} results, top "
                f"{NOTE_BY_KEY[world.key_of[i]].title!r} similarity {s:.3f}"
            )
    if not seen:
        print("neither leg returns anything for any nonsense or off-topic query")


def print_by_distance(
    data: list[Legs], distances: tuple[float, ...], default: Setting
) -> None:
    print("\n== 4a. effect of max_distance alone (weights 1,1,1, current trigram)")
    for dmax in distances:
        m = evaluate(data, default._replace(max_distance=dmax))
        mark = "  <- current" if dmax == default.max_distance else ""
        print("  " + metric_line(m) + mark)


def print_misses(data: list[Legs], world: World, setting: Setting) -> None:
    """Answerable queries whose target is not first (rank shown) under ``setting``."""
    for d in data:
        if d.query.cls not in ANSWERABLE:
            continue
        ranked = fuse(
            [
                (setting.w_lexical, [i for i, _ in d.lexical]),
                (
                    setting.w_title,
                    [i for i, s in d.title if s >= setting.trigram_min],
                ),
                (
                    setting.w_semantic,
                    [i for i, dist in d.semantic if dist <= setting.max_distance],
                ),
            ]
        )[:RESULT_LIMIT]
        rank = next((r for r, i in enumerate(ranked) if i in d.expect_ids), None)
        if rank != 0:
            where = "not in results" if rank is None else f"rank {rank + 1}"
            top = ", ".join(world.key_of[i] for i in ranked[:3]) or "no results"
            print(f"    [{d.query.cls}] {d.query.text!r}: {where}; top: {top}")


def print_grid(
    data: list[Legs],
    world: World,
    distances: tuple[float, ...],
    default: Setting,
    title: str = "4. offline fusion grid",
) -> None:
    print(f"\n== {title}")
    results = grid_search(data, distances)
    n_answerable = sum(1 for d in data if d.query.cls in ANSWERABLE)
    print(f"{len(results)} settings, {n_answerable} answerable queries, k=60")
    ordered = sorted(
        results,
        key=lambda m: (
            m.nonsense_nonempty,
            -m.mrr,
            -m.hit1,
            -m.hit3,
            m.simplicity(default),
        ),
    )
    print("best ten (settings with no nonsense result first, then by MRR):")
    for m in ordered[:10]:
        print("  " + metric_line(m))
    current = evaluate(data, default)
    print("CURRENT setting:")
    print("  " + metric_line(current))
    print("  answerable queries whose target is not first:")
    print_misses(data, world, default)
    clean = [m for m in results if m.nonsense_nonempty == 0]
    if not clean:
        print("no setting keeps every nonsense query empty: no recommendation")
        return
    best_mrr = max(m.mrr for m in clean)
    near = [m for m in clean if m.mrr >= best_mrr - 1.0 / n_answerable]
    simplest = min(near, key=lambda m: (*m.simplicity(default)[:2], m.offtopic_mean))
    same = sorted(
        m.setting.max_distance
        for m in near
        if m.setting[:3] == simplest.setting[:3]
        and m.setting.trigram_min == simplest.setting.trigram_min
    )
    middle = same[len(same) // 2]
    chosen = next(
        m
        for m in near
        if m.setting[:3] == simplest.setting[:3]
        and m.setting.trigram_min == simplest.setting.trigram_min
        and m.setting.max_distance == middle
    )
    print(
        f"best MRR with every nonsense query empty: {best_mrr:.3f}; within one "
        f"query ({1.0 / n_answerable:.3f}) of it: {len(near)} settings"
    )
    print(
        f"simplest of those uses weights "
        f"({simplest.setting[0]:g},{simplest.setting[1]:g},{simplest.setting[2]:g})"
        f" and trigram {simplest.setting.trigram_min:g}; max_distance values that"
        f" qualify with them: {same[0]:g} to {same[-1]:g}"
    )
    print("RECOMMENDED:")
    print("  " + metric_line(chosen))
    print("  answerable queries whose target is not first:")
    print_misses(data, world, chosen.setting)


def print_by_share(
    data: list[Legs], distances: tuple[float, ...], default: Setting
) -> None:
    """The lexical leg's second try (words OR-ed) at each LOOSE_MIN_SHARE: the
    metrics with the constants as they are, and the best setting of the grid."""
    print(
        "\n== 4b. share of the query's words a note needs when no note has all"
        " of them (find.LOOSE_MIN_SHARE)"
    )
    for share in sorted(data[0].lexical_by_share):
        variant = [replace(d, lexical=d.lexical_by_share[share]) for d in data]
        mark = "  <- current" if share == find.LOOSE_MIN_SHARE else ""
        offtopic = sum(len(d.lexical) for d in variant if d.query.cls == OFFTOPIC)
        print(
            f"share {share:g}{mark}: lexical results for off-topic queries {offtopic}"
        )
        print("  constants as they are: " + metric_line(evaluate(variant, default)))
        best = min(
            grid_search(variant, distances),
            key=lambda m: (
                m.nonsense_nonempty,
                -m.mrr,
                -m.hit1,
                -m.hit3,
                m.simplicity(default),
            ),
        )
        print("  best of the grid:      " + metric_line(best))


def timing(
    embedder: Embedder, load_seconds: float, world: World, started: float
) -> None:
    print("\n== 6. timing")
    texts = [q.text for q in QUERIES]
    embedder.embed(["warm up"])
    begin = time.perf_counter()
    for t in texts:
        emb.embed_query(embedder, t)
    per_query = (time.perf_counter() - begin) / len(texts) * 1000
    print(
        f"first embed call in this process (model load + one text): "
        f"{load_seconds:.2f} s"
    )
    print(f"mean time to embed one query: {per_query:.1f} ms over {len(texts)}")
    print(
        f"indexing {len(NOTES)} notes (build_world incl. signing): "
        f"{time.perf_counter() - started:.2f} s"
    )


MEASURE = os.environ.get("RETICULA_SEARCH_MEASURE") == "1"


@pytest.mark.skipif(not MEASURE, reason="set RETICULA_SEARCH_MEASURE=1 to measure")
@pytest.mark.timeout(1800)
@pytest.mark.parametrize("name", ["fastembed", "hash"])
def test_measure_relevance(conn, owner, monkeypatch, name):
    embedder, load_seconds = load_embedder(monkeypatch, name)
    default_distance = float(embedder.max_distance)  # type: ignore[attr-defined]
    distances = REAL_DISTANCES if name == "fastembed" else HASH_DISTANCES
    default = Setting(1.0, 1.0, 1.0, default_distance, find.TRIGRAM_MIN)
    started = time.perf_counter()
    world = build_world(conn, owner, embedder)
    print(f"\n######## embedder: {embedder.model} (dim {embedder.dim}) ########")
    data = collect(conn, world, embedder, monkeypatch)
    sims = print_per_query(conn, world, data, default)
    print_distributions(data, sims)
    print_relative(data)
    print_unwanted_legs(conn, world, data, default.trigram_min)
    print_by_distance(data, distances, default)
    print_grid(data, world, distances, default)
    print_by_share(data, distances, default)
    timing(embedder, load_seconds, world, started)
