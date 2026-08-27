# Phone keyboard layouts

btkey sends key *positions*; the phone applies its own layout.  For typing
that is ideal - it is what makes the design layout-agnostic.  For pasting it
is a problem, because turning a character back into a position needs to know
the layout, and the only one btkey can read locally is the console's.

Using the console keymap as a stand-in works where the two agree.  Sweeping
an iPhone set to Canadian French, against a `cf` console, showed they do not:

* **Nine of the ninety-six** plain and shifted positions disagreed.  `"`,
  `/`, `#`, `|`, `<`, `>` and both guillemets all arrived as something else.
  Pasting had been quietly wrong for those all along.
* The **entire Option level** was missing from the console keymap - 53 of the
  characters worth pasting, including every dash, every curly quote, the
  ellipsis and the euro.  The phone has 46 of them.

So a swept layout is worth having.  `--phone-layout` takes one; putting

```
phone-layout = ~/.config/btkey/iphone-fr-ca.conf
```

in `~/.config/btkey/btkey.conf` means `btkey` alone picks it up thereafter.

## Measuring it

Turn off **Settings > General > Keyboard > Auto-Correction** and **Smart
Punctuation** first, unless you want the file to describe the phone with them
on; see the caveat below.  Then, with btkey running and connected:

1. On the phone, start a mail message to yourself and put the cursor in the
   body.
2. `btkey --learn-layout`, as yourself in another console.  A second btkey
   carries the message to the running one.
3. Wait, hands off the keyboard, about fifteen seconds.  It types sixteen
   lines.  The status line shows a percentage and the console bell rings
   when it is done; `btkey --cancel` abandons it if it is going into the
   wrong place.
4. Send the mail from the phone to yourself.
5. On a machine running btkey, save that mail to a file, `layout.txt`.  The
   raw message is fine: body, headers and all.
6. `btkey --build-layout layout.txt > ~/.config/btkey/mine.conf`, which
   will tell you to run the second pass if the phone has any dead keys.

The capture is one keyboard row per line, four rows at each of four levels,
between two marker lines:

```
11111111
1 112 113 114 115 116 117 118 119 110 11- 11= 11
q 11w 11e 11r 11t 11y 11u 11i 11o 11p 11^11¨11
...
11111111
```

Sixteen lines, and about eight hundred keystrokes: a labelled probe per key
was nearly twice that.  The row itself says which key each result came from,
so nothing has to be labelled, which also means nothing has to be typed as
*text*.  That matters: text would go through the console keymap, and that is
the very mapping the probe exists to check.  A labelled probe works when the
console nearly matches the phone and fails completely for an AZERTY console
against a QWERTY phone, squarely the case a layout file is for.

Each key is followed by a **space** and then a **doubled marker**, so a row
reads back as delimited fields:

| The field | What it means |
| --------- | ------------- |
| a space | the key produced nothing |
| a character, then a space | an ordinary key |
| a character alone | a dead key: it swallowed the space |

The space settles the dead key, and composing with a space is also the
only way to type a bare accent, since by definition no single keystroke
gives one.

The marker is what delimits the field, and it is not optional.  With only
the space, a dead key immediately followed by a key that produces nothing
borrows that key's space and reads as a literal, shifting everything after
it: `^ X ` fits both `[dead, nothing, literal]` and `[literal, dead,
nothing]`, and nothing in the capture says which.  It is doubled because a
single one could be a result (the key that types it is itself probed) and
no field can hold two in a row, being at most a character and a space.

The lines that bound the capture are that same key pressed eight times.
They separate results from mail headers and signatures, and they are also
how the reader learns what character the marker produces: it does not have
to know the layout in order to read the layout.

`--build-layout` needs neither root nor a phone; it is text in, layout out.
It reports how many results it read, how many characters that yielded, and
how many rows it could not resolve, and records the unresolved ones as
comments in the file so they are visible rather than silently absent.

It reports how many results it read, how many characters that yielded, and
anything it could not resolve, recording the last as comments in the file,
so they are visible rather than silently absent.

## Why there are two passes

The second pass needs to know which keys are dead, and that is a property
of the *results* rather than of anything btkey sent.  btkey knows it typed
keycode 26; only the phone knows that produced a dead circumflex rather
than a literal one.  So the first capture has to come back before the
second probe can be aimed.

The alternative is aiming at everything: 48 positions, four levels, and
nine bases is about 1700 probes, five minutes of typing against 1.6, and it
still leaves an ambiguity - a dead key that does not compose with the base
you happened to choose looks exactly like a literal one.

What is *not* required is a restart between them.  The capture is a file,
so `--learn-accents` reads it in the client and sends the running btkey the
list of keys to try.

## The second pass: compositions

A single-keystroke probe cannot see a dead key.  A dead key followed by the
space the sweep types looks exactly like a literal accent character, and
nothing in the capture distinguishes them.

On the iPhone this matters more than it sounds, because the phone puts three
different dead keys on one position (the key right of `P` gives `^` plain,
`¨` shifted and `` ` `` with Option) while the console keymap has them
scattered across three different keys.  Applying the console's positions to
the phone produced garbage: `à` was sent as the console's dead-grave
position followed by `a`, and that position on the phone is a literal `è`,
so it typed **`èa`**.  Twenty-five accented characters were wrong that way,
each producing two characters instead of one.

Dead keys are therefore *measured* rather than guessed at from the shape of
the character, and `--build-layout` writes them into the layout file:

```
dead    26   0x00   # CIRCUMFLEX ACCENT
dead    26   0x02   # DIAERESIS
dead    26   0x40   # GRAVE ACCENT
```

Then, without restarting anything:

```
btkey --learn-accents layout.txt
```

Either file works there: the raw capture from the first pass, which has the
dead keys in it as the keys that swallowed the space after them, or a
layout file already built from it, which records them again as `dead`
lines.  Whichever is at hand.

which reads the first capture, works out which keys to try, and asks the
running btkey to probe each against space and sixteen base letters.  Then
build the layout from both captures at once:

```
btkey --build-layout layout.txt accents.txt > ~/.config/btkey/mine.conf
```

A pair that comes back as two characters did not compose and is dropped.  A
pair that comes back as one is a composition, and gets written as a
two-keystroke entry.  Composing with **space** is worth the probe on its own:
it is the only way to type the bare accent, since no single keystroke can.

Where a character is reachable more than one way (24 of them were, on the
phone measured here) the cheapest wins: fewer keystrokes first, then fewer
modifiers.  So `è` comes from its own key rather than from grave-then-e, and
`@` from Shift rather than from Option.

**A dead key is not one of those ways**, even though the capture shows what
character it produced.  Pressing it alone types nothing and then swallows
whatever comes next, so recording it as a way to type its own accent would
corrupt the character *after* it too: pasting `x^y` would send `x`, a dead
circumflex, and lose the `y` to it.  The bare accent comes from composing
with a space, which is two keystrokes and the only honest answer.  A dead
key with no composition recorded is simply absent from the layout, which is
better than an entry that damages its neighbour.

## Sharing a layout

The sweep and the import are the whole procedure, so a layout is something
anyone can produce for their own phone and keyboard without touching the
code.  `layouts/` holds the one done here, source and result both:
`iphone-fr-ca.layout.capture` and `iphone-fr-ca.accents.capture` as they
came back from the phone, headers stripped (a capture arrives as a mail
message and only the probe belongs in it) and `iphone-fr-ca.conf` built
from them.  So the file can be rebuilt from its source, and a test checks
that it still matches.  Contributing a layout means adding both.

Nothing in the format is Apple-specific.  A different phone, a different
host, a different physical keyboard: the sweep measures whatever is at the
other end of the link.

## The file

```
U+00A9  34   0x40   # COPYRIGHT SIGN
```

Character (literal or `U+XXXX`), then one or more keycode and modifier
pairs: `0x02` shift, `0x40` right Alt.  Two pairs means two keystrokes, a
dead key and then a base:

```
U+00EA  26   0x00  18   0x00   # LATIN SMALL LETTER E WITH CIRCUMFLEX
```

Comments from `#`.  Hand-editable, which is the point: anything the sweep
got wrong or missed can be corrected in place.

Once there is a layout file, it is the whole of what btkey believes about
the phone.  The console keymap is not merged underneath it, and this is
deliberate: btkey sends positions, so a character the console can type and
the layout does not list is a character no position on the phone produces.
Falling back to the console's answer for it does not type that character,
it types whatever the phone has in that position instead.  btkey says how
many it dropped and which they were, and refuses to send them, which is a
failure you can see.

The dead-key compositions are the reason the console keymap used to be
kept, and the accents pass is what replaced that: it measures them on the
phone, and they land in the file as two-keystroke entries.  Skip that pass
on a phone that has dead keys and those characters are simply absent, which
is what the first pass tells you to do something about.

Absence in the file has two meanings, so it says which.  A pair that was
probed and came back as two characters, the bare accent and then the base,
is one the phone refuses to compose, and those are listed:

```
# Probed and refused by the phone, so unreachable by any two
# keystrokes rather than merely unmeasured:
#   ACUTE ACCENT with y n c
```

On the phone measured here the acute composes with a, e, i, o and u and
with nothing else, so `ý` is not reachable by any two keystrokes and no
amount of re-probing will find it.  The diaeresis does take a `y`, which is
why `ÿ` is in the file.  Without that list, a character missing because
nothing asked and one missing because the phone said no look identical.

The exception is the handful of keys whose meaning is the key rather than a
character: Enter, Tab, Space, Backspace, Escape.  Those are positions on
any keyboard, a measured layout has nothing to say about them, and they
come from the console side whether or not there is a layout file.  A layout
file can still override one by naming it as `U+0009` and the like.

## The smart punctuation caveat

iOS rewrites straight quotes as curly ones as they arrive.  So with that
setting on, a capture records what iOS stored rather than what the key
produced, and the apostrophe entry comes back as `’`.

That is not simply an error: while the setting is on, `’` genuinely *is*
what that key yields, and recording it is what makes pasting match.  But it
means the file describes a configuration, not just a layout, so re-measure
after changing it.

It also means no key produces a straight quote, so one cannot be pasted at
all.  Dropping it is the worst outcome available: `c'est` would arrive as
`cest`, a misspelling, where `c’est` is merely a typographic substitution,
and the one iOS was going to make anyway.  So `--build-layout` aliases the
straight form onto the key that produced the curly one.

For the apostrophe that comes out exactly right, because the key really is
a rewritten straight quote and iOS goes on choosing the opening or closing
form by context.  For the double quote it does not: `“` and `”` sit on two
different keys, so they are typographic characters in their own right
rather than rewrites, and the alias always sends the opening one, making
`"oui"` arrive as `“oui“`.

That is what the setting costs, and it is why the layout in `layouts/` was
re-measured with Smart Punctuation off.  With it off both quotes are on
keys of their own, no aliasing happens, and the two entries that used to be
artefacts move to where they belong:

| Character | With it on            | With it off  |
| --------- | --------------------- | ------------ |
| `"`       | aliased onto the `“` key | keycode 52, shifted |
| `’`       | the apostrophe key, rewritten | keycode 40, Option+shift |

The marking follows the setting rather than the character.  With it on, the
curly quotes are marked `SMART`, because they are straight ones that were
rewritten and are right only while it stays on.  With it off they are keys
that genuinely type curly quotes and nothing about them is conditional; it
is the straight ones that are marked `STRAIGHT`, being the entries that
would start arriving curly if the setting were turned back on.  Either way
the file says at the top which state it was measured in, so the marks do
not have to be interpreted.
