# SPDX-License-Identifier: GPL-2.0-only
"""The split-screen console layout.

What matters here is not that the escape sequences are pretty but that the
cursor ends every write parked on the status line.  That is the whole
mechanism: BRLTTY follows the cursor, so if a write ever leaves it
somewhere else, the braille display silently drifts off the message.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btkey import display

ROWS, COLUMNS = 25, 80


class FakeStream:
    """A stream that claims a fixed terminal size."""

    def __init__(self, rows=ROWS, columns=COLUMNS):
        self.chunks = []
        self.rows, self.columns = rows, columns

    def write(self, text):
        self.chunks.append(text)

    def flush(self):
        pass

    def fileno(self):
        return -1

    @property
    def text(self):
        return "".join(self.chunks)

    def reset(self):
        self.chunks = []


def make_display(rows=ROWS, columns=COLUMNS):
    stream = FakeStream(rows, columns)
    screen = display.Display(stream)
    screen.rows, screen.columns = rows, columns
    screen.split = rows >= display.MIN_ROWS
    return screen, stream


class LayoutTest(unittest.TestCase):
    def test_start_reserves_the_last_line(self):
        screen, stream = make_display()
        screen.start()
        # Rows 1..24 scroll; row 25 is the status line.
        self.assertIn("\033[1;24r", stream.text)
        self.assertTrue(stream.text.endswith("\033[25;1H\033[2K"))

    def test_start_positions_the_cursor_explicitly(self):
        """DECSTBM homes the cursor, so the region's end must be set by hand."""
        screen, stream = make_display()
        screen.start()
        region, _, rest = stream.text.partition("\033[1;24r")
        self.assertTrue(rest.startswith("\033[24;1H"))

    def test_close_restores_the_full_screen(self):
        screen, stream = make_display()
        screen.start()
        stream.reset()
        screen.close()
        self.assertIn(display.RESET_REGION, stream.text)
        self.assertTrue(stream.text.endswith("\n"))

    def test_close_is_idempotent(self):
        screen, stream = make_display()
        screen.start()
        screen.close()
        stream.reset()
        screen.close()
        self.assertEqual(stream.text, "")


class WriteTest(unittest.TestCase):
    def setUp(self):
        self.screen, self.stream = make_display()
        self.screen.start()
        self.stream.reset()

    def test_log_leaves_the_cursor_parked(self):
        self.screen.log("routine")
        self.assertTrue(self.stream.text.endswith("\033[25;1H"))

    def test_log_returns_to_the_scrolling_region_first(self):
        self.screen.log("routine")
        self.assertTrue(self.stream.text.startswith(display.RESTORE_CURSOR))
        self.assertIn("routine\n", self.stream.text)

    def test_status_writes_both_places(self):
        """Once into the scrolling log, once onto the fixed line."""
        self.screen.status("connected")
        self.assertEqual(self.stream.text.count("connected"), 2)
        self.assertTrue(self.stream.text.endswith("\033[25;1H"))

    def test_status_clears_the_line_before_rewriting(self):
        self.screen.status("a longer previous message")
        self.stream.reset()
        self.screen.status("short")
        self.assertIn("\033[25;1H\033[2Kshort", self.stream.text)

    def test_overlong_status_is_truncated_not_wrapped(self):
        """A wrapped status line would scroll the region and break the split."""
        self.screen.status("x" * 200)
        line = self.stream.text.rsplit("\033[2K", 1)[1]
        self.assertEqual(len(line.replace("\033[25;1H", "")), COLUMNS)


class IndicatorTest(unittest.TestCase):
    """A standing indicator at the front of the status line.

    It goes first because the cursor is parked on the first character, so
    that is where a braille display lands - the lock state is read before
    the message rather than after it.
    """

    def setUp(self):
        self.screen, self.stream = make_display()
        self.screen.start()
        self.screen.status("connected")
        self.stream.reset()

    def line(self):
        return self.stream.text.rsplit("\033[2K", 1)[1].replace(
            "\033[25;1H", "")

    def test_indicator_precedes_the_message(self):
        self.screen.set_indicator("CAPS")
        self.assertEqual(self.line(), "CAPS connected")

    def test_indicator_survives_a_new_message(self):
        self.screen.set_indicator("CAPS")
        self.stream.reset()
        self.screen.status("disconnected")
        self.assertEqual(self.line(), "CAPS disconnected")

    def test_clearing_the_indicator_leaves_the_message(self):
        self.screen.set_indicator("CAPS")
        self.stream.reset()
        self.screen.set_indicator("")
        self.assertEqual(self.line(), "connected")

    def test_an_unchanged_indicator_writes_nothing(self):
        """A lock key that has not changed must not cost a repaint."""
        self.screen.set_indicator("CAPS")
        self.stream.reset()
        self.screen.set_indicator("CAPS")
        self.assertEqual(self.stream.text, "")

    def test_repainting_does_not_touch_the_log(self):
        """Only the reserved line moves; the scrolling region is untouched."""
        self.screen.set_indicator("CAPS")
        self.assertNotIn(display.RESTORE_CURSOR, self.stream.text)
        self.assertNotIn("\n", self.stream.text)

    def test_repaint_leaves_the_cursor_parked(self):
        self.screen.set_indicator("CAPS")
        self.assertTrue(self.stream.text.endswith("\033[25;1H"))

    def test_indicator_and_message_are_truncated_together(self):
        self.screen.set_indicator("CAPS")
        self.screen.status("x" * 200)
        self.assertEqual(len(self.line()), COLUMNS)


class ResizeTest(unittest.TestCase):
    """The terminal changing size under a running split."""

    def make(self, rows=ROWS):
        screen, stream = make_display(rows)
        # make_display fixes the size by hand; let the display read it back
        # from the stream so that changing it there is a resize.
        screen._size = lambda: (stream.rows, stream.columns)
        screen.start()
        stream.reset()
        return screen, stream

    def test_a_bigger_terminal_moves_the_status_line_down(self):
        screen, stream = self.make()
        stream.rows = 40
        screen.status("important")
        self.assertIn("\033[1;39r", stream.text)
        self.assertIn("important", stream.text)

    def test_shrinking_below_a_split_takes_the_region_back_down(self):
        # Otherwise nothing ever does: start() will not put a region back
        # at this size, and close() leaves it alone once the split is off -
        # so the bottom line stays frozen after btkey exits.
        screen, stream = self.make()
        stream.rows = 2
        screen.status("important")
        self.assertFalse(screen.started)
        self.assertIn(display.RESET_REGION, stream.text)

    def test_shrinking_below_a_split_still_says_what_it_had_to_say(self):
        screen, stream = self.make()
        stream.rows = 2
        screen.status("important")
        self.assertTrue(stream.text.endswith("important\n"))

    def test_growing_back_puts_the_split_up_again(self):
        screen, stream = self.make()
        stream.rows = 2
        screen.status("small")
        stream.reset()
        stream.rows = ROWS
        screen.status("big again")
        self.assertTrue(screen.started)
        self.assertIn("\033[1;24r", stream.text)


class FallbackTest(unittest.TestCase):
    def test_a_screen_too_short_to_split_still_prints(self):
        screen, stream = make_display(rows=2)
        screen.start()
        screen.log("routine")
        screen.status("important")
        self.assertFalse(screen.started)
        self.assertEqual(stream.text, "routine\nimportant\n")

    def test_not_a_terminal_prints_plainly(self):
        stream = FakeStream()
        screen = display.Display(stream)      # fileno() -1, so no winsize
        self.assertFalse(screen.split)
        screen.start()
        screen.status("important")
        self.assertEqual(stream.text, "important\n")


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
