# Design notes

Why btkey works the way it does.  None of this is needed to use it (see
the [README](../README.md) for that) but every piece here was arrived at
by getting it wrong first, and the reasons are worth keeping.

## How it works

* Keyboards are captured with `EVIOCGRAB` on their `/dev/input/event*`
  devices, held only while our console is in the foreground.  A change of
  console is waited on rather than asked about: the VT layer notifies on
  `/sys/class/tty/tty0/active`, so a `POLLPRI` watch on it wakes btkey at
  the switch and at nothing else.  A grabbed device reaches no other
  in-kernel handler, so the console sees nothing while we forward, and the
  VT switch chords stop working: btkey implements Alt+F*n* itself with
  `VT_ACTIVATE`, and Alt+Escape to quit.

  Ctrl+Alt+Escape deliberately does *not* quit: Ctrl+Option is VoiceOver's
  own modifier, so that combination goes to the phone like the rest of its
  family.  Ctrl+Alt+F*n* does switch console, that chord being older than
  any of this.  Alt+Escape was measured on the phone here and does exactly
  what Escape does.
* The first design read `K_MEDIUMRAW` from the tty instead.  That is
  unusable under Fedora's `use_pty` sudoers: `sudo btkey` gets a pty as
  stdin while sudo's parent reads the real VT to relay into it, so opening
  `/dev/ttyN` would mean two readers splitting each keystroke.  evdev has no
  such coupling, and fails safe on top.
* Every keyboard is grabbed or none is, because modifiers and letters
  routinely live on separate devices, and BRLTTY's injector is a device of
  its own.
* Keycodes are relabelled to HID usages positionally and packed into
  boot-protocol keyboard reports; media keys go out on a separate consumer
  control report.
* The SDP record is published through `org.bluez.ProfileManager1`, so
  bluetoothd does not need `--compat`.  btkey binds PSM 17 and 19 itself and
  answers the HIDP control channel (`SET_PROTOCOL`, `GET_REPORT`,
  `SET_REPORT`, `GET_PROTOCOL` and virtual cable unplug) which is what
  Apple's stack expects.
* Pairing declares `DisplayYesNo`, which gets numeric comparison: the same
  six digits on both ends and nothing to type.  The alternative, declaring
  `KeyboardOnly` so the phone offers a passkey, is a race against a window
  iOS closes in about eight seconds.
* The class of device is written with an HCI command, because bluetoothd
  derives it from the registered profile UUIDs and `main.conf` is not
  honoured for it.
* Pasting is the one thing that is not layout-agnostic, so the phone's
  layout is measured rather than assumed: [LAYOUTS.md](LAYOUTS.md).

## Surviving a bad exit

**Dying is safe by construction.**  `EVIOCGRAB` lives on the open file
description, so the kernel releases every keyboard the moment the process
goes away: SIGKILL, OOM kill, segfault, power to the wrong process.  There
is no console mode left mangled and nothing to repair.  This is the main
reason btkey grabs input devices rather than putting the VT into raw
keycode mode.

What the kernel cannot undo is the system `bluetooth.service` being stopped
in favour of our private daemon, or the `DECSTBM` scrolling region reserving
the console's bottom line.  So btkey forks a **guardian** before acquiring
anything: it holds the read end of a pipe, is told what to undo, and waits.
A clean exit dismisses it; any other ending closes the pipe and it restarts
the unit and gives the console its full screen back, reaching the VT
through `/dev/ttyN`, since under sudo our stdout is a pty that dies with us.
It calls `setsid()` and ignores the job-control signals first, and the
parent blocks until that has happened, so a `kill` aimed at the process
group cannot take it along.

**Hanging is the case that needs help**, because a wedged process keeps its
grabs and the keyboard would be inert with no way to type a rescue command.
So btkey heartbeats to the guardian every two seconds, and if it goes quiet
for fifteen the guardian SIGKILLs it, which hands the keyboard straight
back.  Fifteen seconds is well clear of the only thing here that legitimately
blocks for seconds, a stalled outbound Bluetooth connect.

`tests/test_guardian.py` tests all of this for real: it SIGKILLs parents,
kills whole process groups, and wedges a parent to watch the watchdog fire.

One honest cost of grabbing: **`Alt+SysRq` does nothing while btkey is
forwarding**, since a grabbed device bypasses every other in-kernel handler
including the sysrq one.  Killing btkey from another machine or another VT
releases the grab instantly.

## Bluetooth audio

Once the iPhone has bonded, it can also use this machine as a speaker:
VoiceOver included, which is genuinely useful: the phone talks through the
PC's headphones instead of its own speaker.

btkey keeps that working, and keeps **call** audio out of it.

### Media audio yes, call audio no

The two are different profiles.  A2DP carries media in one direction at
decent quality; HFP and HSP carry call audio, bidirectional and narrowband,
and having them means the phone can decide this machine is where a phone
call should go.

HFP and HSP are not bluetoothd's to withhold: WirePlumber registers them
through `org.bluez.ProfileManager1`, so `--noplugin` cannot touch them.
They are dropped in a WirePlumber drop-in instead, which is in
`examples/50-no-hfp.conf` and belongs in
`~/.config/wireplumber/wireplumber.conf.d/`:

```
monitor.bluez.properties = {
  bluez5.roles = [ a2dp_sink bap_sink bap_source ]
  bluez5.hfphsp-backend = none
}
```

### The catch, and what btkey does about it

Dropping HFP quietly takes the media audio with it, for a reason that is
not obvious. The class of device carries service class bits saying what a
device is *for*, and bluetoothd derives them from the registered profile
UUIDs with a fixed table: Headset and Handsfree map to **Audio**, while
A2DP Sink and Source map only to **Rendering** and **Capturing**.  So the
Audio bit (the one a phone looks at when deciding whether this is
somewhere sound can go) is a side effect of the very profiles we removed.

The effect is visible in the adapter's class:

| Configuration | Class | Service bits |
| ------------- | ----- | ------------ |
| Stock, HFP present | `0x006c0104` | Rendering, Capturing, **Audio**, Telephony |
| HFP dropped | `0x000c0104` | Rendering, Capturing |

Nothing in the D-Bus API exposes those bits, so btkey writes the class it
wants with the `HCI_Write_Class_of_Device` command directly, and puts it
back if bluetoothd overwrites it, which it does whenever the UUID set
changes.  The result is peripheral/keyboard with Audio, Rendering and
Capturing set, and Telephony clear.

None of this happens without `--audio=on`.  By default the `a2dp` and
`avrcp` plugins are dropped too, so the machine offers nothing but a
keyboard.  `--class HEX` sets the
major/minor bits if peripheral/keyboard turns out to be the wrong shape to
advertise: `--class 0x000100` makes it a computer again.

### iOS caches this, so changing anything means re-pairing

iOS records what a device advertises when it pairs and does not look again.
That covers **both** the class of device and the set of profiles, and it is
the single most expensive thing to forget about this whole exercise.

It cost two rounds of chasing on the class: the Audio bit was set correctly,
the log confirmed the adapter changed, and the phone went on listing nothing,
because the bond had been formed while that bit was clear.

Then it cost another round on the profiles.  Dropping `a2dp_source` needed a
re-pair too, and pinning the class did not help, because the class was not
what moved.  It held at `0x2c0540` throughout, verified in the log; the UUID
set changed underneath it, and that was enough.

**So: settle the class and the profile set first, then pair.**  Any later
change to either means forgetting the device on the phone and pairing again.
btkey warns when it notices the advertised set has moved since its last run,
which is the cue.

### What ends up advertised

| UUID | Profile | Needed? |
| ---- | ------- | ------- |
| `0x110b` | A2DP Sink | **Yes**, the phone sending audio here |
| `0x110a` | A2DP Source | Dropped. It is what lets *this* machine play out to a Bluetooth headset, which is nothing to do with the phone-to-PC direction |
| `0x110e` | AVRCP | Redundant: btkey already forwards play/pause and volume as HID consumer reports |
| `0x110c` | AVRCP Target | Redundant, same reason |

Trimming them is a WirePlumber roles change rather than a btkey option;
`examples/50-no-hfp.conf` is the drop-in, and `a2dp_source` is already out of
it.  It needed a re-pair, as above.

Pinning the class still earns its place, just not for that.  During the
WirePlumber restart bluetoothd recomputed the class three times inside one
second as endpoints came and went (`0x080104`, `0x000104`, `0x040104`) and
the `PropertiesChanged` watcher put `0x2c0540` back after each.  A five
second poll would have left a wrong class advertised throughout, which is
exactly the state a phone doing discovery could catch.

At startup btkey logs the class before and after it sets it, with the
service bits spelled out, and which audio profiles the adapter advertises,
so this is checkable rather than a matter of faith.

## Keyboard LEDs

On a real Bluetooth keyboard the Caps Lock light is driven by the *host*,
not by the keyboard: the host sends a HID output report and the keyboard
obeys.  **iOS sends them.**

That took a while to establish, because for a long time none arrived and
that was written down here as a property of iOS, plausible enough, since
Apple's own keyboards have no lights to drive.  It was not.  The LED output
item in the report descriptor was malformed: one-bit fields carrying a
logical range of 0 to 255, inherited from the key array above them, because
Logical Minimum and Maximum are global items and nothing reset them.  iOS
was declining to drive an output collection it could not make sense of, and
a host that rejects the collection is indistinguishable from a host that
chooses not to send.  With the range corrected the reports arrive.

The lesson worth keeping: "the phone does not do X" is a conclusion about
the phone drawn from our own wire format, and it is only as good as that
format.  The descriptor now has a parser in the tests for exactly this
reason.

So a real report is the normal case, and it wins outright: from the first
one, inference stops for the rest of the session.  Inference is still there
for a host that really does not send them.  A lock only changes when its
key is pressed, and every one of those passes through btkey on its way to
the phone, so following them is enough.  What inference cannot know is a
lock the phone was already holding when the link came up; btkey assumes all
off at connect, and one press corrects it.

Either way the result drives the physical keyboards' LEDs with `EV_LED`
events, the same as a host would.

The lights have two owners taking turns.  While btkey holds the grab they
show the phone; while another console is in front they show that console,
because the phone's caps state is no business of a console btkey does not
own.  Releasing the grab hands them over, and returning to the foreground
takes them back, as does plugging a keyboard in while the grab is held,
which otherwise arrives showing whatever the console last set.

A light being no use to the person this was written for, the same state also
appears as a standing indicator at the **front of the status line**:

```
CAPS btkey: connected to 8c:85:90:aa:bb:cc
^ cursor parked here
```

It goes first because the cursor is parked on the first character, so that
is what the braille display lands on: the lock state is read before the
message rather than found after it.  It stands there for as long as the lock
is on, across any number of messages, and is repainted on its own without
disturbing the message or the log.  An announcement would have been worse:
it would cost the reader whatever the line was saying, once, and then scroll
away while the lock stayed on.

The console's own LED state is saved when the grab is taken and put back
when it is released, so the phone's caps state does not outlive the link.

## The function row

A phone is built for a keyboard whose top row sends consumer usages, not
F1 to F12: brightness, Exposé, search, playback, volume.  A PC keyboard
sends the function keys, iOS does nothing with them, and the row is dead.
That is the difference the Apple/Windows/Linux switch on a multi-mode
keyboard selects, and `--top-row=media` is the same thing done here.

Which of the two a host wants is asked rather than assumed.  BlueZ exposes
the Device ID record every host publishes as a modalias,
`bluetooth:v004Cp7510d1A60`, whose first field is the company identifier:
`0x004C` is Apple's with the Bluetooth SIG.  So the row is set on
connection, per host, and a phone that publishes no such record keeps the
function keys, that being different from answering "not Apple" but the same
decision.

The mapping is the kernel's own `magic_keyboard_2021_and_2024_fn_keys`
table read backwards, so it is not a matter of taste: the kernel decides
what an Apple top row means when it arrives, and this decides what to send
so that it means the same.  Keycode to keycode rather than keycode to
usage, so a translated F12 travels the path a keyboard's own volume key
already takes, releases and all.

Two of the twelve took work.  Exposé is consumer usage `0x029F`, above the
`0x023C` the consumer report used to declare it could carry; that maximum
is what the phone is told at bond time and never re-reads, so raising it
cost a re-pair.  It went to `0x3FF` rather than to `0x029F`, since the room
is free now and another re-pair is not.  The kernel's F5 is mic mute, which
it reads from the Telephony page and which has no consumer usage at all, so
that key carries Voice Command instead.  It is the one place this deviates,
and it deviates rather than leaving a hole because a key that does nothing
is the worse of the two.

## Typing speed

A HID keyboard reports its whole state each time, so rolling from one key
straight to the next in a single report is what a real one does when a
typist rolls between keys.  Pasted text is queued that way: one report per
character rather than a press and a release, and a run of capitals holds
Shift down throughout instead of releasing and re-pressing it for every
letter.  About a third fewer reports on ordinary prose.

Two cases still get the extra report.  The same key twice running needs one,
because otherwise nothing in the report changes and the host sees a held key
rather than a second press.  And a change of modifiers gets one: releasing
Shift and pressing the next key in the same report is legal, but hosts
differ on whether the old or the new modifiers apply to it, and a wrong
character is worse than a slow one.

The probe is deliberately left uncoalesced.  It is measuring the host's
behaviour, so each key should be presented as plainly as possible rather
than as quickly as possible.

## Text arriving as text

Text reaches the phone only from the console btkey is running on.  A FIFO
would be more convenient and was there for a while, but anything running as
the same user could write to it, which makes it a keystroke injector into
someone's phone with no way to tell where the text came from.  The console
is not a perfect boundary either, but it is one someone has to be sitting
at.

Reading the console at all needed a second mechanism.  BRLTTY's Linux
screen driver
picks how to inject based on the console's keyboard mode: `K_RAW` and
`K_MEDIUMRAW` go through uinput, but `K_XLATE` and `K_UNICODE`, which is
what a normal console is in, go through **`TIOCSTI`**, pushing UTF-8
straight into the tty's input queue.  That never touches the input layer,
so the evdev grab cannot see it.  (Switching the console to `K_MEDIUMRAW`
to force the uinput path does not help either: that branch only handles a
fixed table of function and cursor keys, not arbitrary text.)

So btkey reads those bytes instead, from its own stdin, where `sudo`'s pty
relay delivers them, and types them out as key positions.  Turning
characters back into positions means consulting the console keymap with
`KDGKBENT`, which is the one place btkey is not layout-agnostic.

This is the path a braille display's own keyboard takes, not just a paste,
which makes it the whole of the interface rather than a convenience.  So
everything on a keyboard has to survive the round trip through characters,
and two sorts of key do not go quietly.  A key whose meaning *is* a control
code (Backspace, Escape) comes back as one, and inverting a keymap that
maps keys to characters cannot produce it; those are put back by name.  A
key that produces no character at all (every arrow, Home, End, Delete,
backtab) arrives as an escape sequence, which is not text and cannot be
looked up as any: `escapes.py` decodes those back into key positions, with
their modifiers, in both the Linux console's spelling and the xterm one.
Undecoded they were not merely lost, which would have been survivable.  The
escape went nowhere and the rest of the sequence was typed on the phone as
literal text, so pressing Delete put `[3~` into the message.

Two subtleties there, both of which cost a round of debugging:

* `KDGKBENT` does not return the keysym the kernel stores.  It returns
  `U(x)`, which `vt_do_kdsk_ioctl` defines as `x ^ 0xf000`, so the type
  bias has to be put back before decoding.  Miss that and `KT_LATIN` keys
  still work by coincidence while every letter decodes as a code point from
  some other script: digits and punctuation paste fine, and not one letter
  does.
* Most accented characters are not on a key at all.  On `cf`, `é` is, but
  `è`, `à` and `ç` are dead-key compositions.  btkey reads the kernel's
  composition table with `KDGKBDIACRUC` and emits those as two keystrokes,
  the dead key then the base letter.  Sending the dead key as a position is
  right for the same reason everything else is: the phone has a dead key in
  that position too.

That brings the `cf` keymap to 157 reachable characters.  A character no key
can produce (an em dash, say) is reported by name and skipped.

A pasted newline goes out as **Shift+Enter**, not Enter.  Enter sends the
message in most chat apps, so pasting two lines with it would fire the first
off before the second arrived: destructive, and not something you can
undo.  Shift+Enter
inserts a line break there and behaves as an ordinary newline in a plain
text field, so it is the safer choice in both.  `--paste-enter` switches
back to plain Enter for somewhere like an SSH client, where Enter is meant
to run the line.

This applies only to *pasted* text.  Pressing Enter goes through the
ordinary key path and is untouched.

### Measuring the phone's layout

The console keymap is only a stand-in for the phone's, and the two need not
agree; on the phone measured here they disagreed at nine positions and the
whole Option level was missing.  So it is measured instead, by having btkey
type a probe the phone answers.

That is a document of its own: [LAYOUTS.md](LAYOUTS.md) covers the capture
format, why the labels have to be key positions rather than text, why dead
keys need a second pass, and how to produce a layout for a phone of your
own.
