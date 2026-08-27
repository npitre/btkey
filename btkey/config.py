# SPDX-License-Identifier: GPL-2.0-only
"""A configuration file, so the usual invocation is just `btkey`.

Everything here is an option that could have been typed on the command
line, expressed by its long name without the dashes:

    phone-layout = ~/.config/btkey/iphone.conf
    pairing = confirm
    audio = on

Switches are written positively and set to yes, no, on or off.  Never
`no-audio = no`, which is a double negative nobody should have to read
twice, and never `with-audio` either: the option is named for the thing
rather than for the flag that happens to express it.

The file is turned into arguments and put *before* the real ones, so
anything typed on the command line overrides it.  That means there is only
one place where an option's name, type and help text are defined - the
parser - and a file cannot drift out of step with it.

It is looked for in the invoking user's config directory before /etc,
because btkey runs under sudo and root's home is not where anyone would
think to put it.  Reading a file owned by that user grants nothing: every
line is an option they could have typed themselves, and none of them names
a program to run.
"""

import os
import pwd

from . import fifo

NAME = "btkey.conf"
SYSTEM_PATH = "/etc/btkey/" + NAME

TRUE_WORDS = ("", "yes", "true", "on", "1")
FALSE_WORDS = ("no", "false", "off", "0")


def home_of_invoking_user():
    """The home directory of whoever ran sudo, not root's."""
    owner = fifo.invoking_user()
    if owner is None:
        return os.path.expanduser("~")
    try:
        return pwd.getpwuid(owner[0]).pw_dir
    except KeyError:
        return None


def expand(value):
    """Expand a leading ~ against the invoking user's home, not root's.

    The shell would have done this for anything typed on the command line,
    using the right home; a config file never passes through a shell, and
    os.path.expanduser here would answer /root, since that is who btkey is
    running as.  Which would make every example in the documentation wrong
    in a way that only shows up under sudo.
    """
    if not value.startswith("~/"):
        return value
    home = home_of_invoking_user()
    return os.path.join(home, value[2:]) if home else value


def candidates():
    home = home_of_invoking_user()
    paths = []
    if home:
        config_home = os.environ.get("XDG_CONFIG_HOME")
        if not config_home or not config_home.startswith("/"):
            config_home = os.path.join(home, ".config")
        paths.append(os.path.join(config_home, "btkey", NAME))
    paths.append(SYSTEM_PATH)
    return paths


def find():
    for path in candidates():
        if os.path.isfile(path):
            return path
    return None


def to_arguments(text, flags=(), optional=(), switches=()):
    """Turn `key = value` lines into a list of command-line arguments.

    `flags` maps the positive name of each switch to the option that
    expresses it and the value that should emit it: `audio` is written by
    `--audio`, which is wanted when audio is *on*.

    `optional` names the options whose value may be left empty, because
    empty means something to them.  `log-file =` is the file turned off,
    the same as `--log-file=` on the command line, and an option that
    accepts that has to be told apart from one where a missing value is
    simply a missing value.

    `switches` names the ones that want on or off rather than a string, so
    a word that is neither can be refused here, with the line it is on,
    rather than by the argument parser, which knows only the option.
    """
    arguments, problems = [], []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not key:
            problems.append("%d: no option name" % number)
            continue
        if key in flags:
            option, emit_when = flags[key]
            if value.lower() in TRUE_WORDS:
                wanted = True
            elif value.lower() in FALSE_WORDS:
                wanted = False
            else:
                problems.append("%d: %s wants yes or no, not %r"
                                % (number, key, value))
                continue
            if wanted == emit_when:
                arguments.append(option)
            continue
        if key.startswith("no-") and key[3:] in optional \
                and key[3:] not in switches:
            # A value option turned off by leaving the value out; telling
            # someone to write `log-file = no` would give them a file
            # called "no".
            problems.append("%d: write %s with no value rather than %s"
                            % (number, key[3:], key))
            continue
        if key.startswith("no-") and (key[3:] in flags or key[3:] in switches):
            problems.append("%d: write %s = no rather than %s"
                            % (number, key[3:], key))
            continue
        if key in switches:
            if value.lower() not in TRUE_WORDS + FALSE_WORDS:
                problems.append("%d: %s wants on or off, not %r"
                                % (number, key, value))
                continue
            arguments += ["--" + key, value]
            continue
        if not value:
            if key in optional:
                arguments += ["--" + key, ""]
                continue
            problems.append("%d: %s needs a value" % (number, key))
            continue
        arguments += ["--" + key] + [expand(word) for word in value.split()]
    return arguments, problems


def load(path, flags=(), optional=(), switches=()):
    with open(path, encoding="utf-8") as handle:
        return to_arguments(handle.read(), flags, optional, switches)
