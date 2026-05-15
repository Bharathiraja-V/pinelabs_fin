# Copyright (c) 2026, Pinelabs Fin contributors
# For license information, please see license.txt

"""Seed the three canonical PineLabs Mode of Payment rows on UPGRADE.

Fresh installs are handled by the ``after_install`` hook
(``pinelabs_fin.install.after_install``) — on a clean ``install-app``
Frappe records patches as already-run without executing them, so a
patch alone would never seed the rows on a new site.

This patch exists for sites that *upgrade* from an older version where
the seed never ran. It delegates to the same idempotent function the
install hook uses, so there's a single source of truth.
"""

from pinelabs_fin.install import seed_pinelabs_modes_of_payment


def execute():
	seed_pinelabs_modes_of_payment()
