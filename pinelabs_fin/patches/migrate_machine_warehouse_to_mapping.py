# Copyright (c) 2026, Pinelabs Fin contributors
# For license information, please see license.txt

"""Convert Pinelabs Machine Mapping rows into Pinelabs Settings child rows.

Two responsibilities, both idempotent:

1. **Backfill parent fields** on any pre-existing rows in
   ``tabPinelabs Machine Mapping`` that were created when the doctype was
   standalone (NULL ``parent`` / ``parenttype`` / ``parentfield`` / ``idx``).
   After Frappe's schema sync adds the standard child columns, we point them
   at ``Pinelabs Settings.machine_mappings`` so they appear in the inline
   grid. Without this, those rows are effectively orphaned.

2. **Migrate the legacy ``warehouse`` field** on ``tabPinelabs Machine
   Config``. For each row with a non-empty warehouse, we INSERT a child
   Mapping row (``mapping_type = Warehouse``, ``machine = machine_name``)
   under Pinelabs Settings, then DROP the column. Keeps installs that
   skipped Phase 2 fully covered.

Runs ``post_model_sync`` so:
  - The Mapping doctype is already in its istable=1 form.
  - The standard child columns (parent/parenttype/parentfield/idx) exist.
  - The Settings doctype has the ``machine_mappings`` field defined.
"""

import frappe

PARENT_DOCTYPE = "Pinelabs Settings"
PARENT_FIELD = "machine_mappings"


def execute():
	if not frappe.db.table_exists("Pinelabs Machine Mapping"):
		return

	_ensure_child_columns()
	_backfill_parent_columns()
	_migrate_warehouse_column()


def _ensure_child_columns():
	"""Add the standard child columns if Frappe's schema sync didn't.

	When ``istable`` flips from 0 → 1 on an existing doctype, Frappe's schema
	sync only diffs the doctype's declared fields against the DB schema — the
	standard child columns (``parent``, ``parenttype``, ``parentfield``,
	``idx``) aren't in ``field_order`` so they're missed. Add them by hand
	before the backfill tries to write to them.
	"""
	for col, ddl in (
		("parent", "ADD COLUMN `parent` varchar(140) DEFAULT NULL"),
		("parenttype", "ADD COLUMN `parenttype` varchar(140) DEFAULT NULL"),
		("parentfield", "ADD COLUMN `parentfield` varchar(140) DEFAULT NULL"),
	):
		if frappe.db.has_column("Pinelabs Machine Mapping", col):
			continue
		try:
			frappe.db.sql_ddl(f"ALTER TABLE `tabPinelabs Machine Mapping` {ddl}")
		except Exception as e:
			frappe.logger().warning(
				f"Pinelabs migrate: could not add `{col}` column: {e}"
			)
	# idx is normally there since istable=0 days, but keep this defensive.
	if not frappe.db.has_column("Pinelabs Machine Mapping", "idx"):
		try:
			frappe.db.sql_ddl(
				"ALTER TABLE `tabPinelabs Machine Mapping` ADD COLUMN `idx` int(8) NOT NULL DEFAULT 0"
			)
		except Exception as e:
			frappe.logger().warning(
				f"Pinelabs migrate: could not add `idx` column: {e}"
			)
	frappe.db.commit()


def _backfill_parent_columns():
	"""Adopt orphan Mapping rows into Pinelabs Settings.

	After ``istable`` flips from 0 → 1, the standard child columns
	(``parent`` / ``parenttype`` / ``parentfield`` / ``idx``) exist (we
	guaranteed that in ``_ensure_child_columns``). Pre-existing rows have
	NULL for the parent fields — stamp them with the singleton's name so
	they render in the Settings grid.
	"""
	orphans = frappe.db.sql(
		"""
		SELECT name FROM `tabPinelabs Machine Mapping`
		WHERE parent IS NULL OR parent = ''
		ORDER BY creation ASC, name ASC
		""",
		as_dict=True,
	)
	if not orphans:
		return

	# Find the next idx so we don't collide with rows added through the UI.
	next_idx = (
		frappe.db.sql(
			"""
			SELECT COALESCE(MAX(idx), 0) FROM `tabPinelabs Machine Mapping`
			WHERE parent = %s AND parenttype = %s AND parentfield = %s
			""",
			(PARENT_DOCTYPE, PARENT_DOCTYPE, PARENT_FIELD),
		)[0][0]
		or 0
	)

	for row in orphans:
		next_idx += 1
		frappe.db.sql(
			"""
			UPDATE `tabPinelabs Machine Mapping`
			SET parent = %s, parenttype = %s, parentfield = %s, idx = %s
			WHERE name = %s
			""",
			(PARENT_DOCTYPE, PARENT_DOCTYPE, PARENT_FIELD, next_idx, row["name"]),
		)

	frappe.db.commit()
	frappe.logger().info(
		f"Pinelabs migrate: adopted {len(orphans)} orphan Machine Mapping row(s) "
		f"into {PARENT_DOCTYPE}.{PARENT_FIELD}."
	)


def _migrate_warehouse_column():
	"""Move legacy Machine Config.warehouse data into child Mapping rows."""
	if not frappe.db.table_exists("Pinelabs Machine Config"):
		return
	if not frappe.db.has_column("Pinelabs Machine Config", "warehouse"):
		return

	rows = frappe.db.sql(
		"""
		SELECT machine_name, warehouse
		FROM `tabPinelabs Machine Config`
		WHERE warehouse IS NOT NULL AND warehouse != ''
		""",
		as_dict=True,
	)

	created = 0
	for row in rows:
		machine_name = row.get("machine_name")
		warehouse = row.get("warehouse")
		if not (machine_name and warehouse):
			continue

		existing = frappe.db.exists(
			"Pinelabs Machine Mapping",
			{
				"parent": PARENT_DOCTYPE,
				"parenttype": PARENT_DOCTYPE,
				"parentfield": PARENT_FIELD,
				"machine": machine_name,
				"mapping_type": "Warehouse",
				"reference_name": warehouse,
			},
		)
		if existing:
			continue

		# Skip references that no longer exist (deleted Warehouse) so the
		# new doctype's validate() doesn't reject the row.
		if not frappe.db.exists("Warehouse", warehouse):
			frappe.logger().warning(
				f"Pinelabs migrate: skipping Warehouse mapping for machine "
				f"{machine_name} → {warehouse} (target Warehouse no longer exists)."
			)
			continue

		_insert_child_mapping(
			machine=machine_name,
			mapping_type="Warehouse",
			reference_doctype="Warehouse",
			reference_name=warehouse,
			description=f"Migrated from Pinelabs Machine Config.warehouse on {frappe.utils.nowdate()}.",
		)
		created += 1

	if created:
		frappe.db.commit()
		frappe.logger().info(
			f"Pinelabs migrate: created {created} Pinelabs Machine Mapping child row(s) "
			"from legacy warehouse field."
		)

	# Frappe doesn't auto-drop columns when fields disappear from doctype JSON.
	try:
		frappe.db.sql_ddl(
			"ALTER TABLE `tabPinelabs Machine Config` DROP COLUMN `warehouse`"
		)
		frappe.db.commit()
	except Exception as e:
		frappe.logger().warning(
			f"Pinelabs migrate: could not drop legacy warehouse column: {e}"
		)


def _insert_child_mapping(*, machine, mapping_type, reference_doctype, reference_name, description=""):
	"""Append a new child row directly via SQL.

	Bypasses doc.insert / Document.validate because:
	- Patches run in a different metadata-warming context where ignore_links
	  / ignore_mandatory don't always cover edge cases.
	- The validation we'd want is already handled here: machine_name is read
	  from a real Machine Config row, and reference_name has been existence-
	  checked above.
	"""
	import uuid

	name = uuid.uuid4().hex[:10]
	now = frappe.utils.now()

	# Find next idx for this parent.
	next_idx = (
		frappe.db.sql(
			"""
			SELECT COALESCE(MAX(idx), 0) + 1 FROM `tabPinelabs Machine Mapping`
			WHERE parent = %s AND parenttype = %s AND parentfield = %s
			""",
			(PARENT_DOCTYPE, PARENT_DOCTYPE, PARENT_FIELD),
		)[0][0]
		or 1
	)

	frappe.db.sql(
		"""
		INSERT INTO `tabPinelabs Machine Mapping`
			(name, creation, modified, modified_by, owner,
			 parent, parenttype, parentfield, idx,
			 mapping_type, reference_doctype, reference_name,
			 machine, is_enabled, description)
		VALUES
			(%s, %s, %s, %s, %s,
			 %s, %s, %s, %s,
			 %s, %s, %s,
			 %s, %s, %s)
		""",
		(
			name,
			now,
			now,
			"Administrator",
			"Administrator",
			PARENT_DOCTYPE,
			PARENT_DOCTYPE,
			PARENT_FIELD,
			next_idx,
			mapping_type,
			reference_doctype,
			reference_name,
			machine,
			1,
			description,
		),
	)
