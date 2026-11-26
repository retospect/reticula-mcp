"""Word handles: a note or hub named by two to four short common words.

The word lists are precis's (precis-mcp ``handle_words.py``, copied with
Reto's leave): three curated lists of 128 words, no homophone pairs, nothing
offensive in any combination. They are part of the contract between a note
and its words: editing a list re-maps every handle.

The number is the last 28 bits of the note's address, the random tail of its
UUID, so it never changes and is the same on every server the note reaches.
Read as four base-128 digits from the top: two words name the top 14 bits,
three the top 21, four all 28. Fewer words name more notes; how many a reader
is shown depends on the notes that reader may read (search/handles.py).

  2 words: <adjective> <noun>
  3 words: <colour> <adjective> <noun>
  4 words: <adjective> <colour> <adjective> <noun>
"""

from __future__ import annotations

import re

_COLORS = [
    "amber",
    "azure",
    "beige",
    "black",
    "blue",
    "bronze",
    "brown",
    "coral",
    "cream",
    "crimson",
    "cyan",
    "gold",
    "gray",
    "green",
    "indigo",
    "ivory",
    "jade",
    "khaki",
    "lemon",
    "lilac",
    "lime",
    "maroon",
    "mauve",
    "navy",
    "olive",
    "orange",
    "peach",
    "pink",
    "plum",
    "purple",
    "ruby",
    "rust",
    "scarlet",
    "silver",
    "tan",
    "teal",
    "violet",
    "white",
    "yellow",
    "saffron",
    "copper",
    "cobalt",
    "chrome",
    "denim",
    "ebony",
    "emerald",
    "fuchsia",
    "garnet",
    "hazel",
    "honey",
    "iron",
    "lavender",
    "magenta",
    "mint",
    "mustard",
    "onyx",
    "opal",
    "pearl",
    "pewter",
    "rose",
    "sage",
    "sand",
    "sepia",
    "slate",
    "steel",
    "tangerine",
    "topaz",
    "turquoise",
    "umber",
    "walnut",
    "wheat",
    "wine",
    "zinc",
    "apricot",
    "berry",
    "cedar",
    "charcoal",
    "cherry",
    "chestnut",
    "clay",
    "cocoa",
    "mocha",
    "mahogany",
    "marble",
    "nickel",
    "paper",
    "platinum",
    "pumpkin",
    "quartz",
    "sable",
    "smoke",
    "snow",
    "stone",
    "tawny",
    "tin",
    "velvet",
    "vanilla",
    "cotton",
    "linen",
    "silk",
    "wool",
    "glass",
    "granite",
    "lagoon",
    "pine",
    "moss",
    "ash",
    "dusk",
    "dawn",
    "cloudy",
    "rainy",
    "sunny",
    "icy",
    "sandy",
    "leafy",
    "pebbly",
    "salty",
    "dewy",
    "mossy",
    "rosy",
    "inky",
    "ruddy",
    "sooty",
    "foggy",
    "hazy",
    "glossy",
    "matte",
    "velvety",
]

_ADJECTIVES = [
    "active",
    "brave",
    "bright",
    "brisk",
    "calm",
    "clever",
    "cosy",
    "crisp",
    "curious",
    "daring",
    "eager",
    "early",
    "fancy",
    "fast",
    "fierce",
    "fluffy",
    "friendly",
    "gentle",
    "giant",
    "glad",
    "grand",
    "happy",
    "hasty",
    "heavy",
    "hidden",
    "honest",
    "humble",
    "jolly",
    "keen",
    "kind",
    "large",
    "lazy",
    "light",
    "little",
    "lively",
    "loud",
    "lucky",
    "merry",
    "mighty",
    "modern",
    "narrow",
    "neat",
    "nimble",
    "noble",
    "noisy",
    "odd",
    "old",
    "plain",
    "polite",
    "proud",
    "quick",
    "quiet",
    "rapid",
    "rare",
    "round",
    "royal",
    "shiny",
    "short",
    "shy",
    "silent",
    "simple",
    "sleepy",
    "slim",
    "slow",
    "small",
    "smart",
    "smooth",
    "soft",
    "solid",
    "sour",
    "spare",
    "steady",
    "sticky",
    "stormy",
    "strong",
    "sturdy",
    "sweet",
    "swift",
    "tall",
    "tidy",
    "tiny",
    "tough",
    "true",
    "vast",
    "warm",
    "wide",
    "wild",
    "wise",
    "witty",
    "young",
    "zesty",
    "bold",
    "busy",
    "cool",
    "dusty",
    "fresh",
    "frosty",
    "lonely",
    "misty",
    "mellow",
    "breezy",
    "chilly",
    "clumsy",
    "dizzy",
    "fuzzy",
    "grumpy",
    "jumpy",
    "lanky",
    "agile",
    "alert",
    "artful",
    "bouncy",
    "cheery",
    "chirpy",
    "comfy",
    "dapper",
    "elegant",
    "festive",
    "frank",
    "gleeful",
    "hardy",
    "jaunty",
    "lithe",
    "nifty",
    "peppy",
    "perky",
    "plucky",
    "snappy",
]

_NOUNS = [
    "acorn",
    "anchor",
    "apple",
    "arrow",
    "badger",
    "banjo",
    "basket",
    "beetle",
    "bell",
    "bird",
    "biscuit",
    "boat",
    "bridge",
    "brook",
    "bucket",
    "butter",
    "cabin",
    "camel",
    "candle",
    "canoe",
    "castle",
    "cat",
    "chair",
    "cloud",
    "clover",
    "comet",
    "cricket",
    "crown",
    "daisy",
    "dog",
    "dolphin",
    "donkey",
    "dragon",
    "drum",
    "duck",
    "eagle",
    "falcon",
    "feather",
    "ferret",
    "finch",
    "fish",
    "flute",
    "forest",
    "fox",
    "frog",
    "garden",
    "goat",
    "goose",
    "hammer",
    "harbor",
    "hawk",
    "heron",
    "hill",
    "horse",
    "island",
    "jacket",
    "jaguar",
    "kettle",
    "kitten",
    "ladder",
    "lamp",
    "lantern",
    "lion",
    "llama",
    "maple",
    "meadow",
    "mirror",
    "monkey",
    "moon",
    "moose",
    "mountain",
    "mouse",
    "otter",
    "owl",
    "panda",
    "parrot",
    "pebble",
    "pigeon",
    "pillow",
    "pirate",
    "planet",
    "pony",
    "puffin",
    "rabbit",
    "raven",
    "river",
    "robin",
    "rocket",
    "saddle",
    "sailor",
    "salmon",
    "seal",
    "sheep",
    "shark",
    "spider",
    "squirrel",
    "swan",
    "tiger",
    "toad",
    "tower",
    "train",
    "trout",
    "turtle",
    "valley",
    "violin",
    "walrus",
    "wagon",
    "willow",
    "wolf",
    "zebra",
    "beacon",
    "blossom",
    "bobcat",
    "cactus",
    "cobweb",
    "cookie",
    "dune",
    "fiddle",
    "geyser",
    "glacier",
    "lizard",
    "magnet",
    "muffin",
    "napkin",
    "oyster",
    "pelican",
    "pretzel",
    "teapot",
]

_BASE = 128
assert len(_COLORS) == len(_ADJECTIVES) == len(_NOUNS) == _BASE
assert len(set(_COLORS + _ADJECTIVES + _NOUNS)) == 3 * _BASE

#: Bits of the address a handle reads (four words of seven bits).
BITS = 28
#: Hex digits at the end of an address that hold them.
HEX = 7
_WORD_RE = re.compile(r"[a-z]+")
#: The list of each word, in the order spoken, for 2, 3 and 4 words.
_SPOKEN = {
    2: (_ADJECTIVES, _NOUNS),
    3: (_COLORS, _ADJECTIVES, _NOUNS),
    4: (_ADJECTIVES, _COLORS, _ADJECTIVES, _NOUNS),
}


def number(address: str) -> int:
    """The 28-bit number of an address: its last seven hex digits, as the SQL
    function ``handle_number`` reads them. ValueError when they are not hex."""
    tail = address[-HEX:]
    if len(tail) != HEX or any(c not in "0123456789abcdefABCDEF" for c in tail):
        raise ValueError(f"not a handle tail: {tail!r}")
    return int(tail, 16)


def _digits(n: int, words: int) -> list[int]:
    """The top ``words`` base-128 digits of ``n``, most significant first."""
    return [(n >> (BITS - 7 * (i + 1))) & (_BASE - 1) for i in range(words)]


def to_words(n: int, words: int = 2) -> str:
    """The spoken form of the top ``words`` digits of 28-bit ``n``."""
    if words not in _SPOKEN:
        raise ValueError("2, 3 or 4 words")
    d = _digits(n, words)
    # the first digit is the noun, the second the adjective before it, ...
    order = list(reversed(d))
    return " ".join(lst[x] for lst, x in zip(_SPOKEN[words], order, strict=True))


def to_prefix(spoken: str) -> tuple[int, int] | None:
    """``(prefix, words)`` for a spoken handle: the top ``7 * words`` bits it
    names. None when it is not 2 to 4 words of the right lists in the right
    order. Accepts spaces, hyphens, commas and any case."""
    toks = _WORD_RE.findall(spoken.lower())
    lists = _SPOKEN.get(len(toks))
    if lists is None:
        return None
    digits = []
    for tok, lst in zip(toks, lists, strict=True):
        if tok not in lst:
            return None
        digits.append(lst.index(tok))
    value = 0
    for d in reversed(digits):  # the noun is the top digit
        value = value * _BASE + d
    return value, len(toks)


def bounds(prefix: int, words: int) -> tuple[int, int]:
    """The numbers a ``words``-word handle names: ``low <= n < high``."""
    shift = BITS - 7 * words
    return prefix << shift, (prefix + 1) << shift


def looks_spoken(ref: str) -> bool:
    """Whether ``ref`` is a spoken handle rather than an address or an id."""
    return to_prefix(ref) is not None
