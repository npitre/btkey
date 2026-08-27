# btkey

Turn a Linux text console into a Bluetooth keyboard for a phone or tablet.

Run `btkey` on any virtual terminal.  While that VT is in the foreground,
every keystroke goes to the paired phone instead of to Linux.  Switch away
with Alt+F*n* and the keyboard behaves normally again; switch back and
forwarding resumes.  The foreground VT *is* the on/off switch: there is no
mode to remember and nothing to toggle.

The phone sees a standard Bluetooth HID keyboard, so on iOS everything a
real keyboard does works, including VoiceOver's Ctrl+Option chords and
quick-nav keys.  Text pasted on the Linux side is typed across too.

## Who this is for

btkey is built by and for someone who does not see either screen: a braille
display driven by BRLTTY on the Linux side, VoiceOver on the phone.  That
accounts for choices that would otherwise look like a user interface nobody
finished.  A braille display shows one line at a time, so there is no
colour, no box drawing and nothing that redraws itself.

None of it costs a sighted user anything, and nothing here is conditional
on a screen reader being present, so it need not be restricted to that use
case; anyone is welcome to contribute enhancements.

## Requirements

* BlueZ 5.65 or newer with a Bluetooth Classic controller
* Python 3 with `python3-dbus` and `python3-gobject`
* Root access
* A real text console, not ssh, not tmux, not a terminal emulator

## Installing

```
git clone https://github.com/npitre/btkey
cd btkey
make check
sudo make install
```

Launcher in `/usr/local/bin`, package beside it in `/usr/local/lib/btkey`,
`btkey-trace-input` and `btkey-system-bluetoothd` alongside, examples and
documentation under `/usr/local/share`.  `sudo make uninstall` removes it.

Without `sudo` it installs under `~/.local` instead; `PREFIX=$HOME` puts it
in `~/bin`.  But btkey itself needs root, and sudo will not search there.

Installing is optional.  btkey may be executed directly from its checked-out
repository with `bin/btkey` where the commands below say `btkey`.

## Running it

```
sudo btkey
```

Nothing needs configuring, and nothing it changes outlasts it: while it
runs, btkey takes over `bluetoothd` and hands it back on exit.
[docs/SETUP.md](docs/SETUP.md) covers what it does to the machine.

## Pairing a phone

1. `sudo btkey` on a text console.
2. On the phone: Settings → Bluetooth, and pick **btkey**.
3. It shows six digits and a **Pair** button.  btkey prints the same digits;
   if they match, tap Pair.  Nothing to type.
4. Done.  The phone reconnects on its own afterwards, and pressing a key
   while disconnected makes btkey dial back out.

`--pairing` changes the style if the default does not suit:

| `--pairing`  | What happens on the phone |
| ------------ | ------------------------- |
| `confirm`    | The same six digits on both ends; tap Pair.  The default. |
| `keyboard`   | The phone shows a passkey to type here, against a window iOS closes in about eight seconds. |
| `display`    | btkey shows a passkey to type on the phone. |
| `justworks`  | Nothing to check.  iOS refuses this for a keyboard. |

Alt+F*n* and Alt+Escape keep working during passkey entry, so a
pairing that never completes cannot trap the keyboard.

## Teaching it your phone's layout

btkey can measure your phone's keyboard layout: it types a test pattern
into the phone, and you send the result back for it to read.  Worth doing
once per phone, and it takes a couple of minutes.

It matters for **pasting**.  btkey sends key *positions*, so putting a
character on the phone means knowing which position produces it there, in
every combination the phone offers.  Only the phone can say.

With btkey running and connected:

1. On the phone, start a new mail message to yourself and put the cursor in
   the **body**.
2. On the PC, in another console: **`btkey --learn-layout`**.  Now leave the
   keyboard alone for about fifteen seconds; it is typing into the phone.
   The status line counts up, and the console bell rings when it is done.
3. Send that mail from the phone to yourself.
4. On the PC, open the mail when it arrives and save it to a file,
   `layout.txt`.  The raw message is fine: body, headers and all.
5. **`mkdir -p ~/.config/btkey`**, then
   **`btkey --build-layout layout.txt > ~/.config/btkey/mine.conf`**

If the phone has dead keys (the ones that put an accent on the *next*
letter) step 5 says so and prints the command for a second pass.  It is the
same shape: a fresh mail, **`btkey --learn-accents layout.txt`**, send it,
save it as `accents.txt`, then

```
btkey --build-layout layout.txt accents.txt > ~/.config/btkey/mine.conf
```

Finally, put this in `~/.config/btkey/btkey.conf` so it is used from then
on:

```
phone-layout = ~/.config/btkey/mine.conf
```

Turn off **Settings → General → Keyboard → Smart Punctuation** before
measuring, unless you want the layout to describe the phone with it on.
[docs/LAYOUTS.md](docs/LAYOUTS.md) has the rest; the `layouts/` directory
holds a worked example with the captures it was built from.

## Configuration

`btkey` reads `~/.config/btkey/btkey.conf`, or `/etc/btkey/btkey.conf`:
the config directory of whoever runs the sudo, not root's.  Every line names
an option that could have been typed:

```
phone-layout = ~/.config/btkey/iphone-fr-ca.conf
pairing = confirm
audio = on
```

The command line wins over the file: `--audio=off` answers `audio = on`
without editing anything.  `examples/btkey.conf.example` is a commented
starting point, and `btkey --help` the full list.

## Audio from the phone

With `--audio=on`, the phone may also treat this machine as a speaker, so
its sound comes out of the PC.  Whether it works depends on how the machine
handles Bluetooth audio; [docs/SETUP.md](docs/SETUP.md) says what has to be
in place.

It is off by default.  For music that is fine.  For VoiceOver, the lag may
range from merely acceptable to annoying as feedback arriving a beat after
the key that caused it is tiring.

## Keys btkey keeps for itself

Alt is the console command prefix:

| Key                                  | Effect                          |
| ------------------------------------ | ------------------------------- |
| Alt+F1 … Alt+F12, Ctrl+Alt+F1 … F12  | Switch to that virtual terminal |
| Alt+Escape                           | Quit btkey                      |

Everything else is forwarded verbatim: plain arrow keys, plain Escape,
Ctrl+Option+arrows, Ctrl+Option+Escape and every other modifier
combination.  Holding Alt down and walking through consoles works.

On an Apple host the F1 to F12 row sends brightness, search, playback and
volume instead, that being what such a host acts on.  btkey works out which
kind of host it is talking to when the phone connects; `--top-row=media` or
`--top-row=function` settles it by hand.

## Reading what btkey is doing

The console is split.  Routine progress scrolls in the upper part; the
current important message sits on a reserved bottom line with the cursor
parked on its first character, so BRLTTY's cursor tracking puts the braille
display on the start of it.  A lock-key indicator, `CAPS`, stands ahead of
the message when the phone has one on.  Time-critical prompts ring the
console bell, which BRLTTY monitors.

## When something is wrong

btkey says its version and who started it as its first line, on the
console rather than only in a file.

Console output scrolls and cannot be scrolled back to, so `--log-file`
keeps a copy of everything btkey prints, timestamped and without the escape
sequences.  On its own it means `/run/btkey/log`, `--log-file PATH` puts it
elsewhere, and `--no-log-file` turns it off.

It is on from a checkout and off from an installation, and mode 0600 where
it exists, a pairing passkey being among the things it records.  `--debug`
adds the Bluetooth agent calls and HID control traffic.

If what the machine advertises has changed since the last run btkey says
so, because a phone already paired will not see the change until it is
forgotten and re-paired.

`btkey --quit` from another console stops it, as Alt+Escape does from its
own.  Killing it works too: the keyboard is released immediately and
`bluetooth.service` put back.

## Tests

```
make check
```

A line per file as each finishes, failures at the end rather than buried in
the passes.  `tests/run tests/test_probe.py` does one of them; `python3
tests/test_probe.py -v` names each test as it runs.

None need root, a phone, or a Bluetooth controller.

## Source

| Module | Role |
| ------ | ---- |
| `session.py` | main loop, key handling, chords, reports |
| `btlink.py` | SDP record, pairing agent, L2CAP, the HIDP control channel |
| `evdev.py` | device discovery, grab, key and LED state |
| `btd.py` | the private bluetoothd, and the class of device |
| `probe.py` | measuring the phone's layout |
| `guardian.py` | cleanup and the watchdog, for when btkey dies badly |
| `kbmap.py` | the console keymap, and layout files |
| `typist.py` | text arriving on the console, typed out as key positions |
| `escapes.py` | the keys that arrive as escape sequences rather than text |
| `display.py` | the split console |
| `advertising.py` | what the machine tells the world it is |
| `pairing.py` | the passkey state machine |
| `journal.py` | the log file |
| `fifo.py` | the control FIFO, and who is allowed to write to it |
| `single.py` | the lock that keeps a second btkey from starting |
| `keycodes.py`, `hidspec.py`, `vt.py`, `config.py`, `cli.py`, `__init__.py` | tables, plumbing |

[docs/DESIGN.md](docs/DESIGN.md) explains why the interesting parts are the
way they are.

## Not implemented

* No mouse.
* Bluetooth LE (HID over GATT) is not used; this is Bluetooth Classic.
* One host at a time.

## Licence

GPL version 2.  See [LICENSE](LICENSE); every source file carries an SPDX
tag saying the same.

Copyright (C) 2026 Nicolas Pitre <nico@fluxnic.net>
