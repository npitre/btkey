# SPDX-License-Identifier: GPL-2.0-only
"""Command line: parse the options, then hand over to a Session.

Kept apart from the main loop because it is bulk rather than logic - the
argparse block is a third of the size of the loop it configures, and none
of it is interesting once the process is running.
"""

import argparse
import errno
import glob
import os
import stat
import sys


from . import (__version__, btlink, config, evdev, from_checkout,
               guardian, hidspec, kbmap, pairing, probe, single, vt)
from .session import Session, started_by


def list_devices(extra_paths):
    """What is there, and which of it btkey could actually have.

    Whether a device is free is not visible any other way: nothing in
    /proc or /sys says who holds a grab, so the only way to find out is to
    ask for it and give it straight back.  Worth the asking, because
    "qualifies as a keyboard" and "btkey can use it" are different
    questions and this used to answer only the first - so a keyboard held
    by a hotkey daemon was listed as one btkey would take.
    """
    # Asking discover() rather than repeating part of it.  This used to
    # test only whether a device looked like a keyboard, so the second
    # interface a keyboard keeps its media keys on - which btkey does take,
    # as a companion - was listed as one it would leave alone.
    chosen = {os.path.realpath(device.path): device
              for device in evdev.discover(extra_paths)[0]}
    # The directory in the heading rather than on every line: it is the
    # same eleven characters twenty times over, and the lines are read
    # a cell at a time on a braille display.
    states = grab_states(chosen.values())
    print(row("/dev/input/*", "status", "keyboard"))
    for path in sorted(glob.glob(evdev.DEVICE_GLOB), key=event_number):
        device = chosen.get(os.path.realpath(path))
        if device is not None:
            print(row(short(path), states[device.path], device.name))
            continue
        try:
            other = evdev.InputDevice(path)
        except OSError as exc:
            # The kernel's own words, which are the honest ones: without
            # root this is "permission denied", and a node that went
            # between the listing and the opening is "no such device".
            # Nothing follows them, there being no name to read without
            # opening it.
            print(row(short(path), reason(exc)))
            continue
        print(row(short(path), "ignored", other.name))
        other.close()
    for device in chosen.values():
        device.close()
    return 0


#: One line of --list-devices, heading included so they cannot drift.
ROW = "%-14s %-16s %s"


def row(node, status, name=""):
    """One line, without the padding that follows a short last column.

    Trailing blanks are cells on a braille display, and the last column
    is empty for a node that could not be opened at all.
    """
    return (ROW % (node, status, name)).rstrip()


def short(path):
    """A node without the directory every one of them shares."""
    prefix = evdev.DEVICE_DIRECTORY + "/"
    return path[len(prefix):] if path.startswith(prefix) else path


def event_number(path):
    """Sort event9 before event10, which sorting by name does not."""
    digits = "".join(c for c in os.path.basename(path) if c.isdigit())
    return (int(digits) if digits else -1, path)


def grab_states(devices):
    """What can be taken, what cannot, and who has the ones that cannot.

    Said as a phrase rather than a word: "grab" meant something to
    whoever wrote it and nothing to anyone reading a listing for the
    first time.  A refusal is nearly always another program holding the
    device, and which one is the whole of what anybody wants to know -
    "used by btkey" means the one already running has it and all is
    well, "used by brltty" means go and look at that configuration.

    Having a device open is not the same as holding its grab, and on an
    ordinary machine several processes have every input device open:
    systemd-logind keeps them all for seat management.  Naming the first
    one found blames whichever /proc happened to list first, which is
    how this came to say "used by systemd-logind" for a keyboard BRLTTY
    had.  What settles it is the devices that *did* come: a process
    holding one of those open is demonstrably not what stops a grab, so
    it is not what stopped this one.
    """
    states, busy = {}, []
    for device in devices:
        if device.grab():
            device.ungrab()
            states[device.path] = "available"
        elif device.grab_error == errno.EBUSY:
            busy.append(device)
        else:
            states[device.path] = reason(
                OSError(device.grab_error or 0,
                        os.strerror(device.grab_error or 0)))

    if busy:
        opened = evdev.openers([device.path for device in devices])
        harmless = {name for path, state in states.items()
                    if state == "available" for name in opened[path]}
        for device in busy:
            names = [name for name in opened[device.path]
                     if name not in harmless]
            states[device.path] = ("used by %s" % ", ".join(names)
                                   if names else "in use")
    return states


def reason(exc):
    """An errno as a phrase, to sit among the other phrases."""
    text = exc.strerror or "cannot be opened"
    return text[:1].lower() + text[1:]


#: Control-FIFO command for each thing a second btkey can ask the first to
#: do.  The names are what the person typing them would call it.
# Name on the wire, what to say we asked for, and whether there is a wait
# worth telling the caller about.  Only the probes have one: they type for
# minutes and ring the bell at the end, which is the signal to start
# reading.  The others are done by the time the message is printed.
COMMANDS = {
    "learn_layout": ("learn-layout",
                     "typing the layout probe", True),
    "learn_accents": ("learn-accents",
                      "typing the accent probe", True),
    "cancel": ("cancel",
               "cancelling whatever it was typing", False),
    "quit": ("quit",
             "stopping", False),
}


def accent_candidates(path):
    """What the accent probe should try, from a capture or a layout file.

    This is why the two passes cannot be one: which keys are dead is a
    property of the *results*, which exist only on the phone.  btkey knows
    what it typed; it cannot know that keycode 26 gave a dead circumflex
    rather than a literal one without being told what came out.

    Deciding it here rather than in the running btkey is what saves a
    restart - the file is a file, and the client can read it.

    Either file will do, because both carry the same answer: the raw
    capture from --learn-layout has the dead keys in it as keys that
    swallowed the space after them, and a layout file built from it records
    them again as `dead` lines for exactly this.  Which one is at hand
    depends on how far through the procedure you are, and it is not worth
    making anyone remember which.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        sys.stderr.write("btkey: cannot read %s: %s\n" % (path, exc.strerror))
        return None

    results, _ = probe.parse_capture(text)
    if results:
        candidates = probe.dead_key_candidates(results)
        source = "layout capture"
    else:
        try:
            candidates = [(keycode, modifiers, None)
                          for keycode, modifiers in kbmap.load_dead_keys(path)]
        except (OSError, ValueError) as exc:
            sys.stderr.write("btkey: %s\n" % exc)
            return None
        source = "layout file"
        if not candidates:
            sys.stderr.write(
                "btkey: %s is neither a layout capture nor a layout file "
                "with\n       accent keys in it.  Pass the mail you got back "
                "from\n       --learn-layout, or the file --build-layout made "
                "from it.\n" % path)
            return None

    if not candidates:
        sys.stderr.write("btkey: no accent keys found in %s; nothing to "
                         "probe\n" % path)
        return None
    sys.stderr.write("btkey: %d accent keys to probe, from the %s\n"
                     % (len(candidates), source))
    return candidates


def send_command(path, name, argument=""):
    """Ask an already-running btkey to do something.

    The FIFO is held open read-write by the running btkey, so a missing
    reader is a reliable way of noticing there is nothing to talk to -
    which is a better answer than silently writing into a file nobody will
    ever read.
    """
    command, description, waits = COMMANDS[name]
    if argument:
        command += " " + argument
    # A path that exists is not necessarily a FIFO.  An earlier `echo`
    # into it, before btkey had ever created it, leaves an ordinary file
    # that would accept the write and report success while nobody reads it.
    try:
        if not stat.S_ISFIFO(os.lstat(path).st_mode):
            sys.stderr.write("btkey: %s is not a FIFO; remove it and "
                             "restart btkey\n" % path)
            return 1
    except OSError:
        pass          # let the open below produce the real diagnosis
    try:
        handle = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno == errno.ENXIO:
            sys.stderr.write(
                "btkey: nothing is listening on %s - is btkey running?\n"
                % path)
        else:
            sys.stderr.write("btkey: cannot reach %s: %s\n"
                             % (path, exc.strerror))
        return 1
    try:
        os.write(handle, (command + "\n").encode())
    finally:
        os.close(handle)
    message = "btkey: %s." % description
    if waits:
        message += "  It rings the console bell when done."
    sys.stderr.write(message + "\n")
    return 0


def import_sweep(paths):
    """Build a layout from one or more captures, in whichever order.

    A layout capture and an accent capture are told apart by shape rather
    than by argument order: only the first has the sixteen rows of the
    level probe, and the second cannot be read at all without the accent
    keys the first identified.
    """
    texts = {}
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                texts[path] = handle.read()
        except OSError as exc:
            sys.stderr.write("btkey: cannot read %s: %s\n"
                             % (path, exc.strerror))
            return 1

    results, problems, remaining = [], [], []
    for path, text in texts.items():
        found, trouble = probe.parse_capture(text)
        if found:
            results += found
            problems += trouble
            continue
        remaining.append(path)

    candidates = probe.dead_key_candidates(results) if results else []
    declined = []
    for path in remaining:
        if not candidates:
            sys.stderr.write(
                "btkey: %s has no layout rows in it, and without a layout "
                "capture\n       there is no way to know what its accent "
                "rows are for.  Pass both.\n" % path)
            return 1
        found, trouble = probe.parse_compositions(
            texts[path], candidates, declined=declined)
        if not found:
            sys.stderr.write("btkey: nothing readable in %s\n" % path)
            for line in trouble:
                sys.stderr.write("btkey:   %s\n" % line)
            return 1
        results += found
        problems += trouble

    layout = probe.add_quote_aliases(probe.to_layout(results))
    deads = probe.dead_key_candidates(results)
    composed = sum(1 for steps, _ in layout.values() if len(steps) > 1)
    sys.stdout.write(probe.render(
        layout, source=", ".join(os.path.basename(p) for p in paths),
        missing=problems, deads=deads, declined=declined,
        smart=probe.smart_punctuation(results)))
    sys.stderr.write(
        "btkey: %d results, %d characters (%d composed), %d unresolved\n"
        % (len(results), len(layout), composed, len(problems)))
    if deads and not composed:
        sys.stderr.write(
            "btkey: %d accent keys found and no compositions yet.  Run\n"
            "       btkey --learn-accents %s\n"
            "       and pass that capture to --build-layout alongside it.\n"
            % (len(deads), paths[0]))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="btkey",
        description="Present this console's keyboard to a phone or tablet "
                    "as a Bluetooth HID keyboard.")
    parser.add_argument("--name", default="btkey",
                        help="name shown on the phone (default: btkey)")
    parser.add_argument("--adapter", default=None,
                        help="adapter to use, e.g. hci0 (default: the first)")
    parser.add_argument("--vt", type=int, default=None,
                        help="console to bind to (default: whichever is in "
                             "the foreground at startup)")
    parser.add_argument("--device", action="append", default=[],
                        metavar="PATH",
                        help="also grab this /dev/input/event* device; may "
                             "be repeated")
    parser.add_argument("--list-devices", action="store_true",
                        help="show input devices and which would be grabbed")

    parser.add_argument("--pairing", default="confirm",
                        choices=sorted(pairing.CAPABILITIES),
                        help="pairing style: confirm (default) shows "
                             "matching digits on both ends with nothing to "
                             "type; keyboard makes the phone show a passkey "
                             "to type here, against a window iOS closes in "
                             "about eight seconds; display shows one here to "
                             "type on the phone; justworks asks for nothing "
                             "at all, which iOS refuses over Classic")
    parser.add_argument("--debug", action="store_true",
                        help="log Bluetooth agent calls and HID control "
                             "traffic")
    parser.add_argument("--phone-layout", default=None, metavar="PATH",
                        help="the phone's keyboard layout, which overrides "
                             "the console keymap where the two disagree.  "
                             "Build one with the three options below")

    learning = parser.add_argument_group(
        "learning the phone's keyboard layout",
        "btkey has to know what the phone's keys produce before it can "
        "paste accurately, and only the phone can say.  With btkey already "
        "running and connected, put the cursor in a mail message to "
        "yourself and run --learn-layout; it types a probe, then send that "
        "mail and feed it to --build-layout.  Repeat with --learn-accents "
        "for the dead keys, and pass both captures.  docs/LAYOUTS.md has "
        "the detail.")
    learning.add_argument("--learn-layout", action="store_true",
                          help="ask a running btkey to type the layout probe")
    learning.add_argument("--learn-accents", metavar="PATH",
                          help="ask it to type the accent probe, for the "
                               "accent keys named in PATH: either the "
                               "capture from --learn-layout or a layout "
                               "file built from it")
    learning.add_argument("--cancel", action="store_true",
                          help="ask it to stop typing a probe")
    learning.add_argument("--build-layout", metavar="PATH", nargs="+",
                          help="turn captured probes into a layout file on "
                               "stdout; pass every capture at once")
    parser.add_argument("--quit", action="store_true",
                        help="ask a running btkey to stop, from another "
                             "console.  It releases the keyboard and puts "
                             "bluetoothd back exactly as Alt+Escape does")
    parser.add_argument("--control-fifo", default="/run/btkey/control",
                        metavar="PATH",
                        help="FIFO accepting commands: learn-layout, "
                             "learn-accents, cancel.  This is how "
                             "--learn-layout and friends reach a running "
                             "btkey (default: /run/btkey/control)")
    parser.add_argument("--paste-enter", dest="shift_newline",
                        default=True, action="store_false",
                        help="paste newlines as plain Enter rather than "
                             "Shift+Enter.  The default is Shift+Enter, "
                             "because Enter sends the message in most chat "
                             "apps and would fire off a paste line by line; "
                             "use this for somewhere like an SSH client "
                             "where Enter is meant to run the line")
    parser.add_argument("--class", dest="device_class", default=None,
                        metavar="HEX",
                        help="class of device major/minor bits (default "
                             "0x%06x, peripheral/keyboard)"
                             % hidspec.MAIN_CONF_CLASS)
    log_file = default_log_file()
    parser.add_argument("--log-file", nargs="?", const=DEFAULT_LOG_FILE,
                        default=log_file, metavar="PATH",
                        # Said from the value rather than alongside it, so
                        # the help cannot describe a default that is not
                        # the one in force.
                        help="append a copy of all messages to PATH.  On its "
                             "own it means %s; --no-log-file, or an empty "
                             "PATH, turns it off.  Given neither, %s"
                             % (DEFAULT_LOG_FILE,
                                "it is %s, since this is a source checkout"
                                % log_file if log_file
                                else "it is off, since this is an installed "
                                     "btkey"))
    # The same three answers under the names anyone might reach for.
    parser.add_argument("--with-log-file", dest="log_file",
                        action="store_const", const=DEFAULT_LOG_FILE,
                        help=argparse.SUPPRESS)
    parser.add_argument("--no-log-file", "--without-log-file", dest="log_file",
                        action="store_const", const="",
                        help=argparse.SUPPRESS)
    parser.add_argument("--top-row", choices=("auto", "function", "media"),
                        default="auto",
                        help="what the F1 to F12 row sends.  media sends "
                             "what an Apple keyboard's top row sends, which "
                             "is what an Apple host acts on; function sends "
                             "F1 to F12.  auto, the default, asks the host "
                             "which it is when it connects")
    parser.add_argument("--audio", nargs="?", type=switch, const=True,
                        default=False, metavar="on/off",
                        help="offer this machine to the phone as a speaker "
                             "as well as a keyboard, and ask it to connect "
                             "that too.  Off by default: it works where the "
                             "machine's audio stack allows it, and the lag "
                             "makes VoiceOver tiring to listen to")
    # The same switch under the names anyone might reach for.  Out of the
    # help, which should show one way of saying a thing rather than five.
    parser.add_argument("--with-audio", dest="audio", action="store_true",
                        help=argparse.SUPPRESS)
    parser.add_argument("--without-audio", "--no-audio", dest="audio",
                        action="store_false", help=argparse.SUPPRESS)
    parser.add_argument("--no-reconnect", action="store_true",
                        help="never initiate an outbound connection")
    parser.add_argument("--system-bluetoothd", action="store_true",
                        help="use the running bluetoothd instead of starting "
                             "a private one; it must already have been "
                             "started with --noplugin=input")
    parser.add_argument("--config", metavar="PATH",
                        help="read options from here instead of the usual "
                             "places (%s)" % ", ".join(config.candidates()))
    parser.add_argument("--no-config", action="store_true",
                        help="ignore any configuration file")
    parser.add_argument("--version", action="version",
                        version="btkey " + __version__)
    return parser


def switch(value):
    """A yes/no option value, in any of the spellings a file accepts.

    The words come from the configuration reader rather than a list of
    their own, so `audio = on` in a file and `--audio=on` on the command
    line cannot come to mean different things.
    """
    if value.lower() in config.TRUE_WORDS:
        return True
    if value.lower() in config.FALSE_WORDS:
        return False
    raise argparse.ArgumentTypeError(
        "wants on or off, not %r" % value)


DEFAULT_LOG_FILE = "/run/btkey/log"


def default_log_file():
    """Where messages go unless told otherwise.

    On from a checkout and off from an installation.  The log is a
    developer's tool: it is the only copy of the console output that can be
    read afterwards, which is worth having while working on btkey and is
    just a file of somebody's keystroke history the rest of the time.  It
    records a displayed passkey too, which is why it is 0600, and not
    writing it at all is better still.
    """
    return DEFAULT_LOG_FILE if from_checkout() else ""


def long_options(parser, wanted):
    """The --long names, without the dashes, whose action `wanted` accepts.

    argparse has no public way to ask what it was given, so this is the
    one place that reaches for the private list; the three questions
    below differ only in what they ask of each action.
    """
    return {option[2:] for action in parser._actions     # noqa: SLF001
            for option in action.option_strings
            if wanted(action) and option.startswith("--")}


def optional_values(parser):
    """The options whose value may be left empty in a file.

    An option that takes its value optionally on the command line takes it
    optionally in a file too: `log-file =` is the file turned off, exactly
    as `--log-file=` is.  Read from the parser so the two cannot drift.
    """
    return long_options(parser, lambda action: action.nargs == "?")


def switch_values(parser):
    """Value options that want on or off rather than a string.

    `audio = maybe` is answerable with a file and a line number here,
    where argparse can only say which option it was.  And `no-audio` is
    advised towards `audio = no`, where `no-log-file` has to be advised
    towards an empty value instead: one of them takes a word and the other
    takes a path.
    """
    return long_options(parser, lambda action: action.type is switch)


def flag_options(parser):
    """{positive name: (option, emit when true)} for every switch.

    A file has room for a value where a flag does not, so it names the
    thing itself and says yes or no: `audio`, never `no-audio` nor
    `with-audio`.  The mapping back to whichever flag expresses that is
    worked out from the parser rather than listed here, so a new switch
    cannot be added and then quietly rejected in a file.

    A switch with both spellings, --with-audio and --without-audio, needs
    only one of them here: the file's value is emitted when it differs from
    the default, and the default is the other one.
    """
    # A switch whose name is the negative of an option that takes a value
    # belongs to that option: --no-log-file turns --log-file off, and
    # `log-file` in a file is a path, not a yes or a no.
    valued = long_options(parser, lambda action: action.nargs != 0)

    flags = {}
    for action in parser._actions:                     # noqa: SLF001
        if action.nargs != 0:
            continue
        for option in action.option_strings:
            if not option.startswith("--"):
                continue
            name = option[2:]
            if name in ("help", "version", "no-config"):
                break
            if name.startswith("without-"):
                break            # its partner below carries the key
            if name.startswith("no-"):
                key, emit_when = name[3:], False
            elif name.startswith("with-"):
                key, emit_when = name[5:], True
            else:
                key, emit_when = name, True
            if key in valued:
                # An option that takes a value owns its name in a file:
                # `audio` is on or off there, and `log-file` is a path,
                # whatever the switches spelling the same thing are called.
                break
            flags[key] = (option, emit_when)
            break
    return flags


def arguments_with_config(parser, argv):
    """argv, with any configured options put in front so argv wins."""
    known, _ = parser.parse_known_args(argv)
    if known.no_config:
        return argv
    path = known.config or config.find()
    if path is None:
        return argv
    try:
        configured, problems = config.load(path, flag_options(parser),
                                           optional_values(parser),
                                           switch_values(parser))
    except OSError as exc:
        sys.stderr.write("btkey: cannot read %s: %s\n" % (path, exc.strerror))
        return argv
    for problem in problems:
        sys.stderr.write("btkey: %s:%s\n" % (path, problem))
    return configured + argv


def main(argv=None):
    parser = build_parser()
    if argv is None:
        argv = sys.argv[1:]
    options = parser.parse_args(arguments_with_config(parser, argv))

    if options.device_class is None:
        options.device_class = hidspec.MAIN_CONF_CLASS
    else:
        try:
            options.device_class = int(options.device_class, 0)
        except ValueError:
            sys.stderr.write("btkey: --class wants a number, e.g. 0x000540\n")
            return 1

    # Pure text processing, so it needs neither root nor a phone.
    if options.build_layout:
        return import_sweep(options.build_layout)

    # Talking to an already-running btkey, which owns the FIFO and has
    # handed it to us, so this needs no privilege either.
    if options.learn_accents:
        candidates = accent_candidates(options.learn_accents)
        if candidates is None:
            return 1
        return send_command(
            options.control_fifo, "learn_accents",
            " ".join("%d:%d" % (keycode, mods)
                     for keycode, mods, _ in candidates))
    for name in ("learn_layout", "cancel", "quit"):
        if getattr(options, name):
            return send_command(options.control_fifo, name)

    if os.geteuid() != 0:
        sys.stderr.write(
            "btkey: must run as root - it grabs /dev/input devices and binds "
            "L2CAP PSM %d and %d\n"
            % (hidspec.PSM_CONTROL, hidspec.PSM_INTERRUPT))
        return 1

    if options.list_devices:
        return list_devices(options.device)

    # Before anything that a second btkey would damage: its bluetoothd
    # cannot start, and failing to start it puts the system one back
    # underneath the btkey already running.
    lock, held = single.hold(started_by())
    if lock is None:
        sys.stderr.write("btkey: another btkey is already running%s\n"
                         % (" (%s)" % held if held else ""))
        return 1

    # Probe the console before anything else: it is the cheapest failure
    # and touches nothing.
    try:
        consoles = vt.Consoles(options.vt)
    except vt.NoConsole as exc:
        sys.stderr.write("btkey: %s\n" % exc)
        return 1

    # Fork the guardian before any D-Bus connection or Bluetooth socket
    # exists, so it inherits nothing it would then hold open.
    keeper = guardian.spawn()
    keeper.reset_console_on_death(consoles.vt)
    btlink.use_glib_mainloop()
    try:
        return Session(options, consoles, keeper).run()
    finally:
        keeper.dismiss()
        consoles.close()
