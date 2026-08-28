# Changes

Newest first.

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
