# SPDX-License-Identifier: GPL-2.0-only
"""Linux input keycode -> USB HID usage translation.

The console hands us keycodes, which are positional: KEY_Q is "the key where
Q sits on a US board", regardless of the loaded keymap.  HID usages are
positional in exactly the same way, so this table is a straight relabelling
and no keymap is consulted anywhere.

The practical consequence is that the *iPhone* applies the layout.  With the
Canadian French console keymap here, the iPhone's hardware keyboard layout
must also be set to Canadian French for the characters to line up.
"""

# HID modifier bit positions, in the order the boot report packs them.
MOD_LEFTCTRL = 0x01
MOD_LEFTSHIFT = 0x02
MOD_LEFTALT = 0x04
MOD_LEFTMETA = 0x08
MOD_RIGHTCTRL = 0x10
MOD_RIGHTSHIFT = 0x20
MOD_RIGHTALT = 0x40
MOD_RIGHTMETA = 0x80

# Linux keycode -> modifier bit.  These never occupy a key slot in the report.
MODIFIERS = {
    29: MOD_LEFTCTRL,     # KEY_LEFTCTRL
    42: MOD_LEFTSHIFT,    # KEY_LEFTSHIFT
    56: MOD_LEFTALT,      # KEY_LEFTALT
    125: MOD_LEFTMETA,    # KEY_LEFTMETA
    97: MOD_RIGHTCTRL,    # KEY_RIGHTCTRL
    54: MOD_RIGHTSHIFT,   # KEY_RIGHTSHIFT
    100: MOD_RIGHTALT,    # KEY_RIGHTALT
    126: MOD_RIGHTMETA,   # KEY_RIGHTMETA
}

# Linux keycode -> HID Keyboard/Keypad page (0x07) usage.
KEYBOARD = {
    1: 0x29,    # KEY_ESC
    2: 0x1E, 3: 0x1F, 4: 0x20, 5: 0x21, 6: 0x22,        # 1 2 3 4 5
    7: 0x23, 8: 0x24, 9: 0x25, 10: 0x26, 11: 0x27,      # 6 7 8 9 0
    12: 0x2D,   # KEY_MINUS
    13: 0x2E,   # KEY_EQUAL
    14: 0x2A,   # KEY_BACKSPACE
    15: 0x2B,   # KEY_TAB
    16: 0x14, 17: 0x1A, 18: 0x08, 19: 0x15, 20: 0x17,   # q w e r t
    21: 0x1C, 22: 0x18, 23: 0x0C, 24: 0x12, 25: 0x13,   # y u i o p
    26: 0x2F,   # KEY_LEFTBRACE
    27: 0x30,   # KEY_RIGHTBRACE
    28: 0x28,   # KEY_ENTER
    30: 0x04, 31: 0x16, 32: 0x07, 33: 0x09, 34: 0x0A,   # a s d f g
    35: 0x0B, 36: 0x0D, 37: 0x0E, 38: 0x0F,             # h j k l
    39: 0x33,   # KEY_SEMICOLON
    40: 0x34,   # KEY_APOSTROPHE
    41: 0x35,   # KEY_GRAVE
    43: 0x31,   # KEY_BACKSLASH
    44: 0x1D, 45: 0x1B, 46: 0x06, 47: 0x19, 48: 0x05,   # z x c v b
    49: 0x11, 50: 0x10,                                 # n m
    51: 0x36,   # KEY_COMMA
    52: 0x37,   # KEY_DOT
    53: 0x38,   # KEY_SLASH
    55: 0x55,   # KEY_KPASTERISK
    57: 0x2C,   # KEY_SPACE
    58: 0x39,   # KEY_CAPSLOCK
    59: 0x3A, 60: 0x3B, 61: 0x3C, 62: 0x3D, 63: 0x3E,   # F1..F5
    64: 0x3F, 65: 0x40, 66: 0x41, 67: 0x42, 68: 0x43,   # F6..F10
    69: 0x53,   # KEY_NUMLOCK
    70: 0x47,   # KEY_SCROLLLOCK
    71: 0x5F, 72: 0x60, 73: 0x61,                       # KP7 KP8 KP9
    74: 0x56,   # KEY_KPMINUS
    75: 0x5C, 76: 0x5D, 77: 0x5E,                       # KP4 KP5 KP6
    78: 0x57,   # KEY_KPPLUS
    79: 0x59, 80: 0x5A, 81: 0x5B,                       # KP1 KP2 KP3
    82: 0x62,   # KEY_KP0
    83: 0x63,   # KEY_KPDOT
    85: 0x94,   # KEY_ZENKAKUHANKAKU
    86: 0x64,   # KEY_102ND
    87: 0x44,   # KEY_F11
    88: 0x45,   # KEY_F12
    89: 0x87,   # KEY_RO
    90: 0x92,   # KEY_KATAKANA
    91: 0x93,   # KEY_HIRAGANA
    92: 0x8A,   # KEY_HENKAN
    93: 0x88,   # KEY_KATAKANAHIRAGANA
    94: 0x8B,   # KEY_MUHENKAN
    95: 0x8C,   # KEY_KPJPCOMMA
    96: 0x58,   # KEY_KPENTER
    98: 0x54,   # KEY_KPSLASH
    99: 0x46,   # KEY_SYSRQ (Print Screen)
    102: 0x4A,  # KEY_HOME
    103: 0x52,  # KEY_UP
    104: 0x4B,  # KEY_PAGEUP
    105: 0x50,  # KEY_LEFT
    106: 0x4F,  # KEY_RIGHT
    107: 0x4D,  # KEY_END
    108: 0x51,  # KEY_DOWN
    109: 0x4E,  # KEY_PAGEDOWN
    110: 0x49,  # KEY_INSERT
    111: 0x4C,  # KEY_DELETE
    117: 0x67,  # KEY_KPEQUAL
    119: 0x48,  # KEY_PAUSE
    121: 0x85,  # KEY_KPCOMMA
    122: 0x90,  # KEY_HANGEUL
    123: 0x91,  # KEY_HANJA
    124: 0x89,  # KEY_YEN
    127: 0x65,  # KEY_COMPOSE (Application/Menu)
    139: 0x65,  # KEY_MENU
    183: 0x68, 184: 0x69, 185: 0x6A, 186: 0x6B,         # F13..F16
    187: 0x6C, 188: 0x6D, 189: 0x6E, 190: 0x6F,         # F17..F20
    191: 0x70, 192: 0x71, 193: 0x72, 194: 0x73,         # F21..F24
    128: 0x78,  # KEY_STOP
    129: 0x79,  # KEY_AGAIN
    131: 0x7A,  # KEY_UNDO
    137: 0x7B,  # KEY_CUT
    133: 0x7C,  # KEY_COPY
    135: 0x7D,  # KEY_PASTE
    136: 0x7E,  # KEY_FIND
    138: 0x75,  # KEY_HELP
}

# Linux keycode -> HID Consumer page (0x0C) usage.  iOS acts on these; the
# equivalents on the keyboard page are widely ignored.
CONSUMER = {
    113: 0x00E2,  # KEY_MUTE
    114: 0x00EA,  # KEY_VOLUMEDOWN
    115: 0x00E9,  # KEY_VOLUMEUP
    163: 0x00B5,  # KEY_NEXTSONG
    164: 0x00CD,  # KEY_PLAYPAUSE
    165: 0x00B6,  # KEY_PREVIOUSSONG
    166: 0x00B7,  # KEY_STOPCD
    172: 0x0223,  # KEY_HOMEPAGE
    120: 0x029F,  # KEY_SCALE, the Expose key on an Apple top row
    217: 0x0221,  # KEY_SEARCH
    582: 0x00CF,  # KEY_VOICECOMMAND, which is what an Apple top row
                  # puts on F5 and what a phone calls dictation
    142: 0x0034,  # KEY_SLEEP
    224: 0x0070,  # KEY_BRIGHTNESSDOWN
    225: 0x006F,  # KEY_BRIGHTNESSUP
}

# Named keycodes that more than one module cares about.
KEY_ESC = 1
KEY_BACKSPACE = 14
KEY_ENTER = 28
KEY_CAPSLOCK = 58
KEY_NUMLOCK = 69
KEY_SCROLLLOCK = 70
KEY_KPENTER = 96

# Either Enter counts, wherever one is accepted.
ENTER_KEYS = {KEY_ENTER, KEY_KPENTER}

# Digits, for anywhere a number has to be typed.
DIGIT_KEYS = {2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 6, 8: 7, 9: 8, 10: 9, 11: 0}
KEYPAD_DIGITS = {82: 0, 79: 1, 80: 2, 81: 3, 75: 4, 76: 5, 77: 6,
                 71: 7, 72: 8, 73: 9}


def digit_for(keycode):
    """The digit a key produces, top row or keypad, or None."""
    return DIGIT_KEYS.get(keycode, KEYPAD_DIGITS.get(keycode))


# Function keys, for recognising the VT switch chords.
FUNCTION_KEYS = {
    59: 1, 60: 2, 61: 3, 62: 4, 63: 5, 64: 6,
    65: 7, 66: 8, 67: 9, 68: 10, 87: 11, 88: 12,
}

# Names used in the status display and log messages.
NAMES = {
    1: "Esc", 14: "Backspace", 15: "Tab", 28: "Enter", 57: "Space",
    58: "CapsLock", 69: "NumLock", 70: "ScrollLock", 99: "PrintScreen",
    102: "Home", 103: "Up", 104: "PageUp", 105: "Left", 106: "Right",
    107: "End", 108: "Down", 109: "PageDown", 110: "Insert", 111: "Delete",
    119: "Pause",
}


def key_name(keycode):
    if keycode in NAMES:
        return NAMES[keycode]
    if keycode in FUNCTION_KEYS:
        return "F%d" % FUNCTION_KEYS[keycode]
    return "keycode %d" % keycode


# The function row read as an Apple keyboard's top row, which is what a
# phone is built to receive: those keys send consumer usages rather than
# F1 to F12, and iOS acts on the first and ignores most of the second.
#
# Mapped to the media keycode rather than straight to a usage, so that the
# consumer path already carrying a keyboard's own volume keys carries these
# too, releases included.
#
# Taken from the kernel's own magic_keyboard_2021_and_2024_fn_keys table,
# which is the same question answered in the other direction.
#
# Eleven of the twelve are that table exactly.  Its F5 is KEY_MICMUTE,
# which has no consumer usage at all - the kernel reads it from the
# Telephony page - so it cannot ride this report, and F5 carries Voice
# Command instead.  That is the one place this deviates, and it is a
# deviation rather than a gap because a key that does nothing is worse.
TOP_ROW_MEDIA = {
    59: 224,     # F1  -> KEY_BRIGHTNESSDOWN
    60: 225,     # F2  -> KEY_BRIGHTNESSUP
    61: 120,     # F3  -> KEY_SCALE
    62: 217,     # F4  -> KEY_SEARCH
    63: 582,     # F5  -> KEY_VOICECOMMAND, see below
    64: 142,     # F6  -> KEY_SLEEP
    65: 165,     # F7  -> KEY_PREVIOUSSONG
    66: 164,     # F8  -> KEY_PLAYPAUSE
    67: 163,     # F9  -> KEY_NEXTSONG
    68: 113,     # F10 -> KEY_MUTE
    87: 114,     # F11 -> KEY_VOLUMEDOWN
    88: 115,     # F12 -> KEY_VOLUMEUP
}
