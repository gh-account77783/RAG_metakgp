"""Unit tests for GraphMind components that do not require external services."""

import faulthandler
import os

# Preserve thread stacks when a CI test stalls, including installed-wheel tests.
if os.environ.get('CI') == 'true':
    faulthandler.dump_traceback_later(120, repeat=True)
