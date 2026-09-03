# Changes

Newest first.

## 0.1.7

- btkey uses the console it was started on rather than whichever one is
  in front.  Started from a console you had switched away from, or over
  ssh, it used to take the keyboard of the console someone was actually
  using.  When it cannot tell where it was started it now says so
  instead of guessing, and `--vt` names a console outright.

- The documentation says what btkey needs root for, one item at a time.

## 0.1.6

- btkey works on a Python that was built without Bluetooth support.
  Some distributions ship one, and there btkey used to stop with an
  error about AF_BLUETOOTH before it could do anything at all.

- `make check` no longer touches the machine it runs on when it is run
  as root.  It could stop the system's bluetooth service and install
  btkey into /usr/local while it was only meant to be testing.

## 0.1.5

- Coming back to btkey's console while still holding Alt left Alt held,
  so every letter reached the phone as Alt and the letter until Alt was
  pressed and released again.  New in 0.1.4.

## 0.1.4

- Using btkey could leave the console believing Ctrl was still held,
  after which `r` started searching bash history.  It now waits for the
  keys to come up before taking the keyboard.

- A keyboard that goes away while a key is down no longer leaves that
  key stuck on the phone.

## 0.1.3

- `btkey --list-devices` says what it means.  Every keyboard is
  `available`, `ignored`, or `used by` the program that has it, with the
  directory in a heading rather than repeated down the page.  Working
  out which program has a keyboard turns out to take some care, and it
  named the wrong one at first.

- If something takes a keyboard away from btkey while you are on another
  console - BRLTTY set up mid-session, say - btkey looks again when it
  comes back, and picks up the loopback keyboard that program leaves
  behind rather than simply losing it.

- Holding a key down no longer wakes btkey thirty times a second.  The
  phone does its own repeating, so btkey turns the kernel's off on the
  keyboards it holds, and puts it back afterwards - including if it is
  killed rather than stopped.

## 0.1.2

- btkey costs nothing to leave running.  It used to wake up twenty-five
  times a second to ask whether anything had happened; now it waits in
  poll() until something does, and while you are on another console it
  does nothing at all.

- A keyboard plugged in while btkey is running is picked up about a
  second later.  The wait gives BRLTTY, or anything else that wants that
  keyboard, first go at it.

- The phone's audio comes up sooner, and comes up at all more often.
  btkey asks for it a second after the keyboard connects rather than
  three, and if the phone answers that it is busy it asks again instead
  of giving up on the sound for the rest of the session.

- A phone that could not be marked as trusted now says so, instead of
  quietly asking to be paired again later on.

- The paths btkey runs most often do less work.  Pressing a key while
  the phone is asleep no longer costs a read off the disk each time,
  asking BlueZ anything is a third of the round trips it was, and coming
  back to btkey's console is quicker.

## 0.1.1

- A keyboard plugged in or unplugged is noticed straight away rather than
  within a couple of seconds, and one that arrived while another console
  had the screen is picked up on the way back to btkey's.

- `--list-devices` says whether each device can actually be taken rather
  than whether it looks like a keyboard, and names the ones another
  program is holding.

- A keyboard another program holds is named only under `--debug`.  No
  keyboard at all being available is said plainly, since btkey with
  nothing to read looks exactly like a phone that has stopped listening.

- A device that has gone away is not blamed on another program.

## 0.1.0

Initial public release.
