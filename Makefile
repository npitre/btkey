# SPDX-License-Identifier: GPL-2.0-only
# btkey is a console program that runs as root, not a library, so it
# installs the way one does: a launcher on PATH with the package beside it,
# rather than into site-packages, where pip and the distribution's package
# manager both have opinions and PEP 668 has a veto.
#
#   make check                  run the tests
#   sudo make install           install under /usr/local
#   make install                install under ~/.local, no root needed
#   make install PREFIX=$$HOME   put it in ~/bin and ~/lib instead
#   make uninstall              remove whichever of those it was
#
# DESTDIR is honoured, for staging into a package.

# Where it goes depends on who is installing, since only one of the two can
# write to /usr/local.  A user install is worth having even though btkey
# itself needs root: --learn-layout, --cancel and --build-layout are all
# run as the person at the keyboard rather than as root.
PREFIX ?= $(if $(filter 0,$(shell id -u)),/usr/local,$(HOME)/.local)
DESTDIR ?=

BINDIR ?= $(PREFIX)/bin
LIBDIR ?= $(PREFIX)/lib/btkey
DATADIR ?= $(PREFIX)/share/btkey
DOCDIR ?= $(PREFIX)/share/doc/btkey

PYTHON ?= python3
INSTALL ?= install


MODULES = $(wildcard btkey/*.py)
LAUNCHERS = bin/btkey tools/btkey-trace-input tools/btkey-system-bluetoothd
EXAMPLES = $(wildcard examples/*)
LAYOUTS = $(wildcard layouts/*)
DOCS = README.md CHANGES.md $(wildcard docs/*.md)

.PHONY: help check install uninstall

help:
	@echo "make check          run the tests"
	@echo "make install        install into $(PREFIX)"
	@echo "make uninstall      take it out again"
	@echo
	@echo "PREFIX picks itself: /usr/local under sudo, ~/.local without."
	@echo "PREFIX=\$$HOME puts it in ~/bin; BINDIR= moves just the launchers."
	@echo
	@echo "The repository stays runnable either way: bin/btkey uses the"
	@echo "package next to it, not the installed one."

check:
	@$(PYTHON) tests/run

install:
	$(INSTALL) -d $(DESTDIR)$(LIBDIR)/btkey
	$(INSTALL) -m 644 $(MODULES) $(DESTDIR)$(LIBDIR)/btkey/
	@# Byte-compile now rather than leaving the first root-run of btkey to
	@# scatter a root-owned __pycache__ through $(LIBDIR).  With DESTDIR the
	@# staging prefix is stripped, so the .pyc files name where they will
	@# live rather than where they were built.
	@$(PYTHON) -m compileall -q $(if $(DESTDIR),-s $(DESTDIR) -p /,) \
	    $(DESTDIR)$(LIBDIR)/btkey >/dev/null || true
	$(INSTALL) -d $(DESTDIR)$(BINDIR)
	@for launcher in $(LAUNCHERS); do \
	    name=$$(basename $$launcher); \
	    echo "  $(BINDIR)/$$name"; \
	    sed 's|^LIBDIR = ""|LIBDIR = "$(LIBDIR)"|' $$launcher \
	        > $(DESTDIR)$(BINDIR)/$$name; \
	    chmod 755 $(DESTDIR)$(BINDIR)/$$name; \
	done
	$(INSTALL) -d $(DESTDIR)$(DATADIR)/examples $(DESTDIR)$(DATADIR)/layouts
	$(INSTALL) -m 644 $(EXAMPLES) $(DESTDIR)$(DATADIR)/examples/
	$(INSTALL) -m 644 $(LAYOUTS) $(DESTDIR)$(DATADIR)/layouts/
	$(INSTALL) -d $(DESTDIR)$(DOCDIR)
	$(INSTALL) -m 644 $(DOCS) $(DESTDIR)$(DOCDIR)/
	@$(MAKE) --no-print-directory installed-notes

# Two things that are only worth saying once it is actually in place.
.PHONY: installed-notes
installed-notes:
	@test -n "$(DESTDIR)" && exit 0; \
	$(BINDIR)/btkey --version >/dev/null 2>&1 \
	    || echo "warning: $(BINDIR)/btkey does not run; is $(LIBDIR) readable?"; \
	case ":$$PATH:" in \
	*:$(BINDIR):*) ;; \
	*) echo; \
	   echo "note: $(BINDIR) is not on your PATH, so plain 'btkey' will"; \
	   echo "      not find it.  Add it, or install somewhere that is.";; \
	esac; \
	if [ "$$(id -u)" = 0 ] && [ -r /etc/sudoers ]; then \
	    path=$$(sed -n 's/^[[:space:]]*Defaults[[:space:]].*secure_path[[:space:]]*=[[:space:]]*"\?\([^"]*\)"\?.*/\1/p' \
	        /etc/sudoers | tail -1); \
	    case ":$$path:" in \
	    ::) ;; \
	    *:$(BINDIR):*) ;; \
	    *) echo; \
	       echo "note: sudo's secure_path does not include $(BINDIR), so"; \
	       echo "      'sudo btkey' will not find it.  Either install with"; \
	       echo "      PREFIX=/usr, or add $(BINDIR) to secure_path in"; \
	       echo "      /etc/sudoers.  'sudo $(BINDIR)/btkey' works either way.";; \
	    esac; \
	elif [ "$$(id -u)" != 0 ]; then \
	    echo; \
	    echo "note: this is a user install, and btkey itself needs root."; \
	    echo "      sudo will not look in $(BINDIR), so run it by path:"; \
	    echo "          sudo $(BINDIR)/btkey"; \
	    echo "      The commands that do not need root do work by name:"; \
	    echo "          btkey --learn-layout, --cancel, --build-layout"; \
	fi; \
	exit 0

uninstall:
	rm -rf $(DESTDIR)$(LIBDIR)
	@for launcher in $(LAUNCHERS); do \
	    rm -f $(DESTDIR)$(BINDIR)/$$(basename $$launcher); \
	done
	rm -rf $(DESTDIR)$(DATADIR) $(DESTDIR)$(DOCDIR)
