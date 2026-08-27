# SPDX-License-Identifier: GPL-2.0-only
"""Reading a phone's keyboard layout off the phone itself.

btkey sends key positions and the phone applies its layout, so pasting is
only correct where btkey knows that layout.  The console keymap is a poor
stand-in: measuring an iPhone against a `cf` console found the two
disagreeing at nine of the ninety-six plain and shifted positions, and the
whole Option level missing - which is where the dashes, the quotes, the
ellipsis and the euro live.

The probe types a keyboard row at a time, four rows at each of four levels,
with a space after every key:

    11111111
    1 2 3 4 5 6 7 8 9 0 - = 1
    q w e r t y u i o p ^¨ 1
    ...
    11111111

Sixteen lines and about four hundred keystrokes.  The row says which key
each result came from, so nothing needs a label - which matters for more
than brevity, because a label would have to be typed as *text*, through the
very console keymap the probe exists to check.  Where the console matches
the phone that works; on an AZERTY console against a QWERTY phone the
labels themselves come out garbled and nothing parses at all.

Each key is followed by a space and then a doubled marker, so a row reads
back as fields separated by that marker:

    a space                the key produced nothing
    a character, a space   an ordinary key
    a character alone      a dead key: it swallowed the space

The space is what settles a dead key - composing with one is also the only
way to type a bare accent, since by definition no single keystroke gives
one.  The marker is what delimits the field, and it has to be there.  With
only the space, a dead key immediately followed by a key that produces
nothing borrows that key's space and reads as a literal, shifting
everything after it: "^ X " fits both [dead, nothing, literal] and
[literal, dead, nothing], and nothing in the capture says which.

The marker is doubled because a single one could be a result: the key that
types it is itself probed.  No field can contain two in a row, being at
most a character and a space, so a doubled marker is unambiguous.  The same
key, pressed eight times, makes the lines that bound the capture - which is
also how the reader learns what character it produces, without having to
know the layout to read the layout.

Dead keys are therefore measured rather than guessed at from the shape of
the character, which would take a literal degree sign or comma for one -
both of which a real keyboard has.
"""

import unicodedata

from . import keycodes

# The alphanumeric block as it sits on the keyboard, one tuple per row.
ROWS = (
    (2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13),          # 1234567890-=
    (16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27),  # qwertyuiop[]
    (30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41),  # asdfghjkl;'`
    (43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 86),  # \zxcvbnm,./ and 102nd
)

# A line of these marks where the capture begins and ends.  Eight presses of
# one key: distinctive whatever that key turns out to produce.
SENTINEL_POSITION = 2
SENTINEL_LENGTH = 8
SPACE_POSITION = 57

# Levels, in the order the capture visits them.  Right Alt rather than left:
# iOS maps both to Option, but the right one is what a PC layout calls
# AltGr, which is the level being probed.
LEVELS = (
    ("L1", 0),
    ("L2", keycodes.MOD_LEFTSHIFT),
    ("L3", keycodes.MOD_RIGHTALT),
    ("L4", keycodes.MOD_RIGHTALT | keycodes.MOD_LEFTSHIFT),
)

# What to try composing each dead key with.  Space first, since a dead key
# followed by a space is how the bare accent itself is typed.
BASES = (
    (57, 0),                                        # space
) + tuple((keycode, 0) for keycode in
          (30, 18, 23, 24, 22, 21, 49, 46)          # a e i o u y n c
) + tuple((keycode, keycodes.MOD_LEFTSHIFT) for keycode in
          (30, 18, 23, 24, 22, 21, 49, 46))         # the same, shifted

# iOS rewrites straight quotes as curly ones as they arrive, so these are
# what it stored rather than what the key produced.  Correct for pasting
# while Smart Punctuation is on, wrong once it is off - worth marking either
# way.  Only single characters can be affected: the substitutions needing
# several, "--" to a dash and "..." to an ellipsis, cannot happen here.
SMART_QUOTES = "‘’“”"
#: And the straight ones it replaces.
STRAIGHT_QUOTES = "'\""


def terminator_strokes():
    """What ends a line: Shift+Enter.

    Shift rather than plain Enter for the same reason pasting uses it - in
    anything that treats Enter as send, a probe would be posted line by
    line.  Both are positions, so neither depends on the layout.
    """
    return [(keycodes.MOD_LEFTSHIFT, keycodes.KEYBOARD[keycodes.KEY_ENTER])]


# -- reading a capture back -----------------------------------------------

def dead_key_candidates(results):
    """The keys that swallowed their space, so are dead keys.

    Measured rather than inferred: the capture says outright which ones ate
    the space that followed them.
    """
    return [(steps[0][0], steps[0][1], char)
            for steps, char, notes in results
            if len(steps) == 1 and "dead" in notes]

#: A straight quote and the typographic ones iOS may put in its place.
#: Cheapest variant first is not assumed - it is worked out - but the
#: apostrophe is listed before the opening single quote because that is
#: overwhelmingly what the key is used for.
QUOTE_FAMILIES = (("'", "\u2019\u2018"), ('"', "\u201c\u201d"))


def add_quote_aliases(layout):
    """Let a straight quote be typed by the key that yields a curly one.

    With Smart Punctuation on, iOS rewrites straight quotes as it stores
    them, so no key produces one and a straight quote is unpasteable.
    Dropping it is the worst outcome available: "c'est" arrives as "cest",
    a misspelling, where "c’est" is merely a typographic substitution -
    and the one iOS was going to make anyway.

    So the straight form is aliased onto the key that produced the curly
    one.  Where that key is a rewritten straight quote, which is the usual
    case, iOS goes on choosing the opening or closing form by context and
    the result is exactly right.
    """
    for straight, curlies in QUOTE_FAMILIES:
        if straight in layout:
            continue                      # a real one exists; nothing to do
        available = [(char, layout[char]) for char in curlies
                     if char in layout]
        if not available:
            continue
        char, (steps, notes) = min(available, key=lambda pair: _cost(pair[1][0]))
        layout[straight] = (steps, list(notes) + ["arrives as %s" % char])
    return layout


def _cost(steps):
    """Fewer keystrokes first, then fewer modifiers."""
    return (len(steps), sum(bin(mods).count("1") for _, mods in steps))

def to_layout(results):
    """{character: (steps, notes)}, cheapest way of typing it winning.

    A character reachable more than one way should be typed the cheap way:
    one keystroke beats two, and no modifier beats a modifier.  That also
    settles the accented letters, which a phone may offer both as a direct
    key and as a dead-key composition.

    A dead key is *not* one of those ways, even though the capture shows
    what character it produced.  Pressing it alone types nothing and then
    swallows whatever comes next, so recording it as a way to type its own
    accent would corrupt the character after it as well: pasting "x^y"
    would send x, a dead circumflex, and then lose the y to it.  The bare
    accent has to come from composing with a space, which is two
    keystrokes and the only honest answer.
    """
    layout = {}
    for steps, char, notes in results:
        if "dead" in notes:
            continue
        # The probe covers the alphanumeric block, so a space here came
        # from some key that happens to emit one - a curiosity, not a way
        # to type a space.  Space, tab and newline have keys of their own,
        # which are the same on every layout and are filled in by
        # kbmap.whitespace.
        if char in " \t\n":
            continue
        current = layout.get(char)
        if current is None or _cost(steps) < _cost(current[0]):
            layout[char] = (steps, notes)
    return layout

def render(layout, source="a sweep", missing=(), deads=(), declined=(),
           smart=None):
    """The layout file, as text."""
    lines = [
        "# A phone's hardware keyboard layout, read off the phone itself.",
        "#",
        "# btkey sends key positions and the phone applies its layout, so",
        "# pasting is only correct where btkey knows that layout.  The console",
        "# keymap is only a stand-in for it, and the two need not agree.",
        "#",
        "# Generated from %s by btkey --build-layout." % source,
        "# Format: <character or U+XXXX>  <linux keycode>  <hid modifier bits>",
        "#   0x02 = shift, 0x40 = right alt (Option)",
        "#",
        "# A character may need two keystrokes, a dead key and then a base;",
        "# those lines carry two keycode and modifier pairs.",
        "#",
    ] + smart_notes(smart)
    for keycode, modifiers, accent in deads:
        lines.append("dead    %-3d  0x%02x   # %s"
                     % (keycode, modifiers, unicodedata.name(accent, accent)))
    if deads:
        lines.append("")
    for problem in missing:
        lines.append("# unresolved: %s" % problem)
    if missing:
        lines.append("")
    for line in declined_note(declined):
        lines.append(line)
    if declined:
        lines.append("")
    for char in sorted(layout, key=lambda c: (_cost(layout[c][0]),
                                              layout[c][0])):
        steps, notes = layout[char]
        comment = unicodedata.name(char, "unnamed")
        if notes:
            comment += "  [" + "; ".join(notes) + "]"
        keys = "  ".join("%-3d  0x%02x" % pair for pair in steps)
        lines.append("U+%04X  %s   # %s" % (ord(char), keys, comment))
    return "\n".join(lines) + "\n"

def smart_notes(smart):
    """The header lines about Settings > Keyboard > Smart Punctuation.

    Which quote entries are conditional depends on which way that setting
    was turned when the capture was taken, so the file records that rather
    than leaving the reader to work it out from what is marked.
    """
    if smart is None:
        return [""]
    if smart:
        return [
            "# Smart Punctuation was ON when this was measured.  Lines marked",
            "# SMART came back curly because iOS rewrote a straight quote on",
            "# the way in, so they are right only while it stays on.",
            "#",
            "# A straight quote is then aliased onto the same key, since no",
            "# key produces one while that setting is on.  Dropping it would",
            "# turn \"c'est\" into \"cest\", a misspelling, where \"c\u2019est\" is",
            "# only a typographic substitution - and the one iOS was going to",
            "# make.",
            "",
        ]
    return [
        "# Smart Punctuation was OFF when this was measured, so the curly",
        "# quotes here are keys that really do type them and nothing about",
        "# them is conditional.  Lines marked STRAIGHT are the other way",
        "# round: turn that setting on and they will start arriving curly.",
        "",
    ]


def declined_note(declined):
    """Comment lines for the pairs the phone was asked about and refused.

    Absence in this file has two possible meanings, and they are not the
    same: never probed, or probed and answered no.  Writing the second one
    down is what makes the difference readable without going back to the
    capture and decoding it by hand.
    """
    if not declined:
        return []
    merged = {}
    for accent, bases in declined:
        # Upper and lower case of a base are two probes of one question.
        seen = merged.setdefault(accent, [])
        for base in bases:
            if base.lower() not in seen:
                seen.append(base.lower())
    lines = ["# Probed and refused by the phone, so unreachable by any two",
             "# keystrokes rather than merely unmeasured:"]
    for accent, bases in merged.items():
        lines.append("#   %s with %s"
                     % (unicodedata.name(accent, accent), " ".join(bases)))
    return lines


def _press(keycode, modifiers=0):
    return (modifiers, keycodes.KEYBOARD[keycode])

def _sentinel_strokes():
    return ([_press(SENTINEL_POSITION)] * SENTINEL_LENGTH
            + terminator_strokes())

def _field_strokes(keycode, modifiers):
    """One probed key: the key, a space to settle it, then the delimiter."""
    return [_press(keycode, modifiers),
            _press(SPACE_POSITION),
            _press(SENTINEL_POSITION),
            _press(SENTINEL_POSITION)]


def _row_strokes(positions, modifiers):
    """One keyboard row at one level."""
    strokes = []
    for keycode in positions:
        strokes += _field_strokes(keycode, modifiers)
    return strokes + terminator_strokes()

def capture_strokes():
    """The whole layout probe: four rows at each of four levels."""
    strokes = list(_sentinel_strokes())
    for _, modifiers in LEVELS:
        for positions in ROWS:
            strokes += _row_strokes(positions, modifiers)
    return strokes + _sentinel_strokes()

def compose_strokes(candidates, bases=BASES):
    """One row per candidate dead key, composed with each base."""
    strokes = list(_sentinel_strokes())
    for dead_key, dead_mods, _ in candidates:
        if dead_key not in keycodes.KEYBOARD:
            continue
        for base_key, base_mods in bases:
            if base_key not in keycodes.KEYBOARD:
                continue
            strokes.append(_press(dead_key, dead_mods))
            strokes.append(_press(base_key, base_mods))
            strokes.append(_press(SPACE_POSITION))
            strokes.append(_press(SENTINEL_POSITION))
            strokes.append(_press(SENTINEL_POSITION))
        strokes += terminator_strokes()
    return strokes + _sentinel_strokes()

def _is_sentinel(line):
    line = line.strip()
    return (len(line) >= SENTINEL_LENGTH - 2
            and not line[0].isspace()
            and len(set(line)) == 1)

def capture_block(lines):
    """(rows, marker character) between the sentinels, or None.

    The marker comes from the sentinel itself, which is that same key
    pressed eight times - so the reader learns what it produces on this
    keyboard without having to know the layout in order to read the
    layout.
    """
    marks = [index for index, line in enumerate(lines) if _is_sentinel(line)]
    if len(marks) < 2:
        return None
    return lines[marks[0] + 1:marks[-1]], lines[marks[0]].strip()[0]

def read_row(text, count, marker):
    """Decode one row into `count` entries of (text, is dead).

    The whole field is returned, not its first character: a composition
    probe that did not compose comes back as the accent *and* the base,
    and only the caller knows how many characters it was expecting.  A
    missing entry - the row ran out - comes back as (None, False), the
    same as a key that produced nothing.  Both are honest: the capture
    does not say.
    """
    entries = []
    for field in text.split(marker * 2)[:count]:
        if field == " ":
            entries.append((None, False))          # only the space came back
        elif field.endswith(" "):
            entries.append((field[:-1], False))    # the output and its space
        elif field:
            entries.append((field, True))          # the space was swallowed
        else:
            entries.append((None, False))
    return entries + [(None, False)] * (count - len(entries))

def parse_capture(text):
    """Read a row-per-line capture.  Returns (results, problems)."""
    found = capture_block(text.splitlines())
    if found is None:
        return [], []
    block, marker = found
    expected = [(positions, modifiers)
                for _, modifiers in LEVELS for positions in ROWS]
    if len(block) != len(expected):
        return [], ["expected %d rows between the markers, found %d"
                    % (len(expected), len(block))]

    results, problems = [], []
    for line, (positions, modifiers) in zip(block, expected):
        for keycode, (char, dead) in zip(
                positions, read_row(line, len(positions), marker)):
            if char is None:
                continue
            if len(char) != 1:
                problems.append("keycode %d gave %r, which is not one "
                                "character" % (keycode, char))
                continue
            notes = ["dead"] if dead else []
            results.append((((keycode, modifiers),), char, notes))
    annotate_quotes(results)
    return results, problems


def smart_punctuation(results):
    """Whether iOS was rewriting straight quotes when this was captured.

    Deduced rather than asked.  With the setting on, every straight quote
    typed into the phone comes back curly, so a straight one anywhere in
    the capture means it was off.  A layout with no straight-quote key at
    all would read as "on", which is the safe way round: it is what makes
    a straight quote get aliased onto a curly key, and having one that
    arrives curly beats not having one at all.
    """
    return not any(char in STRAIGHT_QUOTES for _, char, _ in results)


def annotate_quotes(results):
    """Mark the quote entries that depend on the Smart Punctuation setting.

    Which ones those are depends on the setting itself, and marking the
    wrong ones is worse than marking none.  With it on, a curly quote in
    the capture is a straight one that iOS rewrote, and the entry is right
    only while it stays on.  With it off, curly quotes are keys that really
    do type curly quotes and nothing about them is conditional - it is the
    straight ones that would start arriving curly if it were turned on.
    """
    on = smart_punctuation(results)
    wrong_way_round = SMART_QUOTES if on else STRAIGHT_QUOTES
    for _, char, notes in results:
        if char in wrong_way_round:
            notes.append("SMART" if on else "STRAIGHT")

# What each base keycode types, for naming a pair that did not compose.
BASE_LETTERS = {57: "space", 30: "a", 18: "e", 23: "i", 24: "o",
                22: "u", 21: "y", 49: "n", 46: "c"}


def base_letter(keycode):
    return BASE_LETTERS.get(keycode, "keycode %d" % keycode)


def parse_compositions(text, candidates, bases=BASES, declined=None):
    """Read a compose capture, whose rows follow the candidate order.

    `declined` collects (accent, bases) for the pairs that were probed and
    came back as two characters.  Those are not failures of the probe: the
    phone was asked and answered that it does not compose them, and the
    file is where that answer belongs, or the next person to wonder whether
    a missing character was ever probed has to decode a capture to find out.
    """
    if declined is None:
        declined = []
    found = capture_block(text.splitlines())
    if found is None:
        return [], []
    block, marker = found
    usable = [c for c in candidates if c[0] in keycodes.KEYBOARD]
    if len(block) != len(usable):
        return [], ["expected %d accent rows between the markers, found %d"
                    % (len(usable), len(block))]

    results = []
    for line, (dead_key, dead_mods, accent) in zip(block, usable):
        refused = []
        for (base_key, base_mods), (char, _) in zip(
                bases, read_row(line, len(bases), marker)):
            # Two characters back means the accent and the base both came
            # through, so the pair did not compose and there is nothing to
            # record.  One means it did.
            if char is None or len(char) != 1:
                refused.append(base_letter(base_key))
                continue
            results.append((((dead_key, dead_mods), (base_key, base_mods)),
                            char, []))
        if refused:
            declined.append((accent, refused))
    return results, []
