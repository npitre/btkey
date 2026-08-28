# What btkey does to the machine

**btkey needs no system configuration.**  `sudo btkey` works on a stock
machine, and nothing it changes there outlasts it: the files it writes are
its own, listed below.  This describes what it does while it is running,
and the one permanent alternative.

For btkey's own settings (which layout to use, how to pair) see
`examples/btkey.conf.example` and `btkey --help`.

## bluetoothd

BlueZ ships an `input` plugin implementing the HID *host* role, so the
machine can drive Bluetooth keyboards and mice.  That plugin registers UUID
`0x1124` and binds L2CAP PSM 17 and 19 at startup.

btkey needs to be the HID *device* on the same controller, which means
owning that same UUID and those same PSMs.  There is no way to share them:
with the plugin loaded, registering the profile fails with

```
org.bluez.Error.NotPermitted: UUID already registered
```

and binding the PSMs fails with `EADDRINUSE`.

So on startup btkey stops `bluetooth.service`, remembering whether it was
running, and starts `bluetoothd --nodetach --noplugin=input` in its place.
On exit it terminates that and starts the unit again.  The private daemon
reads the same `/etc/bluetooth/main.conf`; nothing under `/etc` is written
at any point.

While btkey runs, the machine cannot use Bluetooth input devices of its
own: no Bluetooth keyboard, mouse, or braille display.  That reverts the
moment btkey exits.

Older guides say to put `DisablePlugins = input` in `main.conf`.  That is
BlueZ 4 syntax and has no effect on BlueZ 5.

Only one btkey can do this, so only one may run.  A second would find
`org.bluez` taken, fail to start its own daemon, and in undoing itself
start `bluetooth.service` underneath the one already running.  It takes a
lock on `/run/btkey/lock` before touching anything and says who has it
instead.

## Class of device

The class says what kind of device this is, and a phone reads it to decide
whether to offer the machine as a keyboard and as somewhere to send sound.
bluetoothd derives the service class bits from the registered profile
UUIDs, and its table maps Headset and Handsfree to **Audio** while A2DP
maps only to Rendering and Capturing, so offering media audio without call
audio leaves the one bit a phone looks for switched off.

Nothing in the D-Bus API exposes those bits, and `main.conf`'s `Class` is
not honoured either: a run with `Class = 0x000540` in it left the adapter at
the default `0x0c0104`.  So btkey writes the class with the
`HCI_Write_Class_of_Device` command directly, and puts it back whenever
bluetoothd recomputes it, which it does on every change to the UUID set,
three times inside one second during a WirePlumber restart.

`--class` sets the major and minor bits.  Without `--audio=on` the audio
bits are not set at all, and the `a2dp` and `avrcp` plugins are dropped
with them.

**iOS caches all of this when it pairs and never looks again**, so changing
either the class or the profile set means forgetting the device on the phone
and pairing afresh.  btkey notices when what it advertises has moved since
the last run and says so.

## Bluetooth audio, without call audio

Once a phone has bonded it can also use the machine as a speaker, which is
useful: VoiceOver comes out of the PC's headphones rather than the phone's
speaker.  Call audio is a different matter: HFP and HSP would let the phone
decide this machine is where a phone call should go.

Those are not bluetoothd's to withhold.  WirePlumber registers them through
`org.bluez.ProfileManager1`, so `--noplugin` cannot touch them.  They are
dropped with a WirePlumber drop-in instead: `examples/50-no-hfp.conf`,
which belongs in `~/.config/wireplumber/wireplumber.conf.d/`:

```
monitor.bluez.properties = {
  bluez5.roles = [ a2dp_sink bap_sink bap_source ]
  bluez5.hfphsp-backend = none
}
```

`a2dp_source` is dropped there too.  It is what lets *this* machine play out
to a Bluetooth headset, which has nothing to do with the phone-to-PC
direction; put it back in the list if this machine ever needs Bluetooth
headphones of its own.

Doing this costs the `Audio` bit in the class of device, since bluetoothd
derives that from the profile UUIDs and only Headset and Handsfree map to
it.  btkey puts the bit back itself (see above) so the phone still offers
the machine as a place to send sound.  [DESIGN.md](DESIGN.md) has the whole
story; it took three rounds to work out.

### Asking for the audio channel

Pairing a keyboard and routing audio are separate decisions to a phone,
and it makes the second one by connecting a second profile.  It does not
always make it: after a fresh bond iOS connects HID and stops there, so
this machine advertises somewhere to send sound and nothing ever asks it
to.  Everything else is in place at that point, the class of device and
WirePlumber's A2DP Sink endpoints both, and the audio simply does not
arrive with nothing anywhere saying why.

So with `--audio=on`, a second after the keyboard connects, btkey asks
the phone to connect its A2DP Source profile as well and logs what came
back.  Not in the same breath, because a phone still bringing up the
keyboard answers that it is busy.

That answer is a request to come back rather than a refusal, so btkey
asks again, doubling the wait each time: at one second, three, seven and
fifteen, until the phone either takes it or says something that is an
actual answer.  A phone that does not offer A2DP at all says so
differently and is left alone.  Nothing here can cost more than the
audio.

## Keyboards

btkey holds an `EVIOCGRAB` on every keyboard while its console is in the
foreground, and lets go the instant it is not.  `btkey --list-devices`
shows what it would take:

```
/dev/input/event0      -      Power Button
/dev/input/event1      grab   AT Translated Set 2 keyboard
/dev/input/event5      held   Some USB keyboard
/dev/input/event6      grab   Some USB keyboard Consumer Control
/dev/input/event7      -      Some USB keyboard System Control
/dev/input/event4      grab   BRLTTY Linux Screen Driver Keyboard
```

`grab` is a device btkey would take, `held` one another program has, and
`-` one it leaves alone.  Finding out costs a grab and an immediate
release, since nothing in `/proc` or `/sys` says who holds one; run it
while btkey is running and everything btkey has shows as `held`, by
btkey.

A device qualifies only if it can produce the whole letter block plus Enter
and Space.  That admits real keyboards and BRLTTY's uinput injector, which
is what makes pasting reach the phone, while leaving the power button, the
ACPI video bus and the lid switch alone, none of which should be taken away
from the local machine.  `--device PATH` adds anything the filter misses.

A keyboard usually presents more than one device: the letters on one, the
volume and media keys on another that cannot pass that test.  Those are
taken as well when they share a physical path with a keyboard that did,
since they are the same keyboard, and otherwise their keys would go on
working here while btkey has the rest of it.  Not the System Control
interface, though: it carries Power, Sleep and Wake, which belong to this
machine, and grabbing it would leave the power key doing nothing.

Nothing here needs cleanup: the grab is a property of the open file
description, so the kernel drops it when btkey exits, however it exits.

### When a keyboard will not come

The kernel keeps one grab per device: `input_grab_device` refuses a second
with `EBUSY`, and while a grab is held `input_pass_values` delivers to the
holder alone and to no other handler.  That is what makes the grab worth
taking, and it is also why two programs cannot share one: there is no
arrangement where both get a copy.

The same holds one level down, between two programs sharing the evdev
node: `evdev_events` passes to the grabbing client alone, or to every open
descriptor when there is no grab.  So a hotkey daemon holding a keyboard
does not merely stop btkey taking it, it stops btkey seeing anything from
it: btkey has the device open and reads nothing.

What happens to those keys is then entirely the holder's business.  A
daemon that swallows what it wants and replays the rest through `uinput`
leaves a second, virtual keyboard for btkey to find and grab, and typing
works normally through that; one that replays nothing leaves the keyboard
doing only whatever that daemon does with it.

BRLTTY does exactly the first when it is set up to put braille commands on
the ordinary keyboard, and the listing shows the whole arrangement:

```
/dev/input/event1      held   AT Translated Set 2 keyboard
/dev/input/event4      grab   BRLTTY 6.9.1 Keyboard Instance - event5
/dev/input/event5      held   CM Storm QuickFire Rapid keyboard
/dev/input/event6      grab   CM Storm QuickFire Rapid keyboard Consumer Control
/dev/input/event14     grab   BRLTTY 6.9.1 Keyboard Instance - event1
```

Both real keyboards are BRLTTY's, and each has an instance beside it
carrying what BRLTTY did not keep.  btkey takes the instances, so it gets
those keys as key positions, and the braille commands never reach it,
which is right: they were meant for BRLTTY.

Whatever holds it may let go later.  The grab is retried on every return
to the foreground, and both the refusal and the eventual success are
reported under `--debug`, since a keyboard that quietly changes which of
the two paths it takes is indistinguishable from btkey misbehaving.  Only
under `--debug`, because a machine with BRLTTY or a hotkey daemon on it
has something sitting on a device every session and nothing is wrong;
naming them on a console that is a few lines of braille would be noise.

The exception is no keyboard coming at all, which is said plainly.  btkey
with nothing to read looks exactly like a phone that has stopped
listening, and those are chased in entirely different places.

### Noticing the console change

Keystrokes go to the phone exactly while btkey's console is in front, so
btkey has to know when that stops being true, and quickly: until it does
it still holds the keyboard the other console is being typed at.

The VT layer notifies on `/sys/class/tty/tty0/active`, which names the
console in front, so the question is waited on with `POLLPRI` instead of
asked.  Asking is what btkey used to do, 25 times a second for as long as
it ran.  A kernel without the attribute falls back to that.

The attribute has to be read once before it is watched, and again after
every wakeup: an unread sysfs attribute is ready from the outset, so a
watch that never reads it fires immediately and forever.  The read is a
`pread` from offset zero, because what clears the readiness is a read
that starts at the beginning; one carrying on from where the last one
stopped is past the end and returns nothing.

Its presence is all that is checked.  Waiting on it has worked for as
long as it has existed, and a probe would prove nothing anyway: an unread
sysfs attribute reports itself ready whether or not anything notifies on
it, so one would pass on a kernel that never says a word.

The way this can fail is a descriptor that goes into error, which
is then reported ready for ever: a callback that says "carry on" turns
the watch into a busy loop.  The ordinary notification is `POLLPRI` and
`POLLERR` together, so only an error *without* the event, or a read that
fails, counts as one, and either ends the watch in favour of asking.
That one is BRLTTY's lesson from monitoring `/dev/vcsa`.

Leaving the foreground also closes the keyboards rather than merely
letting go of them.  Ungrabbing is not enough to go quiet: an open device
delivers everything typed on it either way, so btkey would wake for every
keystroke meant for the console that actually has the screen, only to
throw it away.  They are opened again, and anything that was unplugged
meanwhile is dropped, on the way back.

Only the ones that came are kept.  A keyboard btkey holds no grab on is
either somebody else's, and then it delivers nothing at all, or nobody's,
and then its keys reach the console too and arrive here a second time as
text; either way the descriptor buys nothing.  It is opened and tried
again on the next switch back, so one that comes free is not lost.

The watch on `/dev/input` follows the foreground as well: what is plugged
in while another console has the screen is that console's business, and
the set is looked at afresh on the way back regardless.

The order matters in both directions.  The watch goes on *before* the
directory is looked at, because a keyboard plugged in between the two
would fall through the gap: too late for the look, too early for a watch
that did not exist yet, and unnoticed until the next switch.  It comes
off *before* the keyboards are given back, for the same reason the other
way round: an arrival reported after we have let go would have btkey open
and grab a device on a console that is no longer its own.

So does the watchdog.  The guardian exists to SIGKILL a btkey whose main
loop has stopped turning, because the kernel then releases the keyboard
grabs; with no grabs held there is nothing to release, and a wedged
background btkey is a process doing nothing rather than a machine that
cannot be typed at.  It is told to stand down on the way out and armed
again on the way in, which also means the process makes no periodic
noise at all while it is not the one being typed at.

The beat is five seconds against a ten second deadline.  A beat is not
something that goes missing: the timer fires unless the main loop has
stopped turning, and the only call that blocks inside the running loop is
the send to the phone, which blocks while holding the keyboard.  So the
deadline has to cover scheduling jitter and nothing more.  Everything
else that can take its time - waiting for bluetoothd, restarting a unit,
putting the adapter back - happens either before the watchdog is armed or
after it has been stood down, which the teardown does first for that
reason.

### Noticing a keyboard arriving

A keyboard plugged in while btkey is running is picked up without it,
because the `/dev/input` directory is watched rather than looked at every
so often.  The short wait before looking matters as much as the watch: the
node appears before udev has given it its ownership and mode, so a look
the instant it is created finds something btkey is not allowed to open,
and one keyboard arrives as a burst of several nodes.

The wait is a second, which is longer than either of those needs, because
it is also whatever else wants this keyboard getting first refusal.  A
program that grabs it for its own hotkeys publishes the keys it does not
want through uinput, and that loopback is the device btkey should be
holding rather than the keyboard itself; BRLTTY does exactly this.
Looking too soon means taking the real keyboard out from under it, or
finding the loopback not yet created and missing it until the next
console switch.  Each arrival restarts the wait, so the loopback
appearing is itself what ends it.

A keyboard that arrives while another console has the screen is left alone
until the switch back, which is also when the set is looked at afresh:
what is plugged in can change while btkey is not the one being typed at.

### Which way a key came in

Keys reach btkey two ways, and they are not equivalent.  From evdev they
are forwarded as *key positions*, so any key works whether or not it
produces a character.  From the console they arrive as *text*, and have to
be turned back into positions: printable characters by looking them up in
the keymap, control codes by name, and keys that produce no character at
all (the arrows, Home, End, Delete) by decoding the escape sequence they
arrive as.

**BRLTTY's braille keyboard takes the second path.**  Its uinput device
carries the keys of a physical keyboard, but braille typing is written to
the console as text, so on a braille display everything goes through the
decoding above.  That is measured rather than assumed:
`sudo btkey-trace-input`, run with btkey stopped, watches both paths
at once and labels each keystroke with the one it came in on.  Stopped,
because a device btkey has grabbed delivers to btkey and to nobody else.

It is worth having when a key does nothing, because btkey cannot report
that by itself: a key that never arrives looks exactly like a key that was
never pressed.

## The console

The console is split with `DECSTBM`, as apt's fancy progress does it: rows
1..n-1 scroll, row n is reserved for the current important message, and the
cursor is parked on that message's first character.  BRLTTY tracks the
cursor, so the braille display ends up on the start of the text without the
reader having to hunt for it, and a reserved line cannot scroll out from
under it.

Two consequences worth knowing:

* `DECSTBM` homes the cursor on the Linux console, so btkey positions it
  explicitly afterwards rather than relying on where it lands.
* Nothing in the kernel undoes a scrolling region.  btkey resets it on exit,
  and the guardian resets it if btkey is killed, reaching the VT through
  `/dev/ttyN` rather than stdout, since under sudo that stdout is a pty that
  dies along with the process.  A console left with a frozen bottom line is
  otherwise a puzzle; `printf '\033[r'` or `reset` is the manual fix.

Time-critical prompts, the pairing dialogs, also write `\a`.  BRLTTY
monitors the console bell, so those arrive as an audible cue without btkey
producing any audio itself.

Everything btkey prints also goes to `/run/btkey/log`, timestamped and
without the escape sequences, which is the only copy that can be read after
the fact.  From a checkout that happens by default; an installed btkey
writes nothing unless `--log-file` asks it to.

## Text, and why it needs a layout

btkey is otherwise entirely layout-agnostic: keys arrive as positions and
leave as positions, and the phone applies the layout.  Text is the
exception, because BRLTTY delivers it as characters through `TIOCSTI`
rather than as key events, so btkey has to work out which key produces
each one.

Without a measured layout it falls back to inverting the console keymap
with `KDGKBENT`, which is only right where the console and the phone agree,
and they need not.  Measuring the phone is the answer;
[LAYOUTS.md](LAYOUTS.md) is the procedure.

## Files

| Path | What it is |
| ---- | ---------- |
| `/run/btkey/lock` | held while btkey runs, so a second one refuses to start |
| `/run/btkey/log` | everything btkey printed, timestamped; mode 0600, since a displayed passkey goes through it.  Only from a checkout: an installed btkey writes no log unless `--log-file` asks |
| `/run/btkey/control` | write commands here; `btkey --learn-layout`, `--quit` and friends do |
| `/var/lib/btkey/host` | the last host that paired, so a key press can dial it |
| `/var/lib/btkey/advertised` | what was last advertised, to notice when it changes |

The control FIFO is handed to whoever invoked the sudo, so `btkey
--learn-layout` and friends work without a second one, and to nobody else.
It is set to mode 0600 on every start, not only when btkey creates it, since
one left behind by an earlier run keeps whatever mode that run left it with;
and the result is checked with `fstat` after opening rather than assumed.
If it is reachable by anyone else, btkey says so and does not listen on it,
which costs the learn commands and nothing else.

That check is worth its few lines because anything able to write there
causes keystrokes on a phone.  It is a narrow channel (the commands take
key positions, not text, so it cannot spell arbitrary things) and a loud
one, since a probe takes seconds, shows a percentage and rings the bell.
But narrow and loud is not the same as closed.

Text can only be typed from the console btkey was started on.  There was
once a FIFO for it as well, which anything running as the same user could
write to: a keystroke injector into someone's phone, with nothing to say
where the text came from.  Reading only from that console means the text
has to come from someone sitting at it.

## The permanent alternative

```
sudo btkey-system-bluetoothd
sudo btkey --system-bluetoothd
```

This installs a `bluetooth.service` drop-in running
`bluetoothd --noplugin=input`, and tells btkey to use the running daemon
rather than starting its own.  The upside is a marginally faster start and
no bluetoothd restarts.  The cost is that **the machine permanently loses
the Bluetooth HID host role**, whether or not btkey is running.  Prefer the
default unless there is a specific reason.

`sudo btkey-system-bluetoothd --undo` reverts it.
