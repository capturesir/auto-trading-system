"""Paper account ids. ``DU``, one optional letter, then digits."""

from __future__ import annotations

import re

DU_ACCOUNT = re.compile(r"^DU[A-Z]?[0-9]+$")
