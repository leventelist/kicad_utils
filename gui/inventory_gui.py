#!/usr/bin/env python3
"""Component inventory GUI.

Manage the electronic component database used by the KiCad database library
and bomer2.py: search and browse, add / edit / duplicate / delete components,
manage categories and suppliers, track stock with a logged and undoable
history, deduct BOMs, print QR code labels and handle scanned codes (own
labels and distributor bags). Before the first change in a session a backup
copy is written to `backups/` next to the database file. See README.md.

Usage: inventory_gui.py [path/to/db.sqlite]
"""

import hashlib
import html
import os
import shutil
from contextlib import contextmanager
from datetime import datetime
import shlex
import sqlite3
import subprocess
import sys
import tempfile

import bom_csv
import distributor_codes

from PySide6.QtCore import (QDate, QMarginsF, QRectF, QSettings, QSize, QSizeF, Qt,
                            QTimer, Signal, qVersion)
from PySide6.QtGui import QGuiApplication, QPageSize, QPdfWriter
from PySide6.QtGui import (QAction, QColor, QFont, QImage, QKeySequence,
                           QPainter, QPixmap, QStandardItem,
                           QStandardItemModel)
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QLineEdit,
                               QMainWindow, QMenu, QMessageBox, QCompleter, QPlainTextEdit, QPushButton,
                               QSpinBox, QSplitter, QTableView, QTableWidget,
                               QTableWidgetItem, QTabWidget, QToolBar,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget, QAbstractItemView, QDateEdit, QListWidget,
                               QListWidgetItem)

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "db", "db.sqlite")
LABEL_PREFIX = "KC:"
KICAD_TOOL = 2

# Niimbot B21s: 203 dpi = 8 px/mm, 48 mm printable width -> 384 px.
LABEL_W, LABEL_H = 384, 240
# Label sizes: (name, width px, height px). The name is stored in the
# settings, so existing names must not change. 50 mm labels are printed on
# the 48 mm wide print head.
LABEL_SIZES = [("50 × 30 mm (B21s)", 384, 240), ("40 × 30 mm", 320, 240),
               ("50 × 15 mm", 384, 120)]


def label_size_px(name):
    for n, w, h in LABEL_SIZES:
        if n == name:
            return w, h
    return LABEL_SIZES[0][1:]
# Printer command = executable + arguments, run in the working directory.
# niimprint (github.com/AndBondStyle/niimprint) has no executable of its own:
# it runs as `python3 -m niimprint` from inside its checkout (its dependencies
# pyserial, pillow and click must be installed for that python). The label is
# sent unrotated; this is the setup tested on a B21S over USB. niimprint needs
# the fix that waits for the print status before ending the job, otherwise
# labels are cut off after ~20 mm.
PRINTER_DEFAULTS = {
    "executable": "python3",
    "arguments": "-m niimprint -m b21 -c usb -a /dev/ttyACM0 -i {image}",
    "workdir": "~/git/niimprint",
    # Optional: pad labels narrower than the print head (B21: 384 px = 48 mm)
    # to the full head width, centred, for printers that print narrow images
    # left-aligned. Not needed for 384 px wide labels.
    "center": False,
    "head_width": 384,
}
PRINT_TIMEOUT = 120  # seconds per label
MAX_RECENT = 8
APP_NAME = "Component inventory"
PROJECT_URL = "https://github.com/leventelist/kicad_utils"
# Editable columns. device: plain columns; cad_data: (column, label).
DEVICE_FIELDS = ["value", "value2", "value3", "technology", "type",
                 "description", "documentation", "comment"]
CAD_FIELDS = [("symbol", "Symbol"), ("footprint", "Footprint"),
              ("mech_model", "3D model"), ("short_footprint", "Short footprint (JLC)"),
              ("Description", "Description"), ("Keywords", "Keywords"),
              ("Footprint Filters", "Footprint filters")]
SUPPLIER_FIELDS = [("name", "Name"), ("homepage", "Homepage"), ("address", "Address"),
                   ("city", "City"), ("country", "Country"),
                   ("contact_person", "Contact person"), ("email", "E-mail"),
                   ("phone", "Phone"), ("portable", "Mobile"), ("fax", "Fax"),
                   ("type", "Type"), ("comment", "Comment")]
UNDO_PREFIX = "undo of #"
REQUIRED_TABLES = {"device", "category", "cad_data", "source", "supplier", "stock"}


# --------------------------------------------------------------------------
# Data access
# --------------------------------------------------------------------------

class Database:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.backup_path = None
        self.on_backup = None  # callback(path), set by the GUI
        # mode=rw: never create a new file. isolation_level=None: transactions
        # are handled explicitly in write().
        self.con = sqlite3.connect(f"file:{path}?mode=rw", uri=True,
                                   isolation_level=None)
        self.con.row_factory = sqlite3.Row
        # Fails with sqlite3.DatabaseError if the file is not an SQLite db.
        tables = {r["name"] for r in self.con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
        missing = REQUIRED_TABLES - tables
        if missing:
            self.con.close()
            raise ValueError("not a component database, missing table(s): "
                             + ", ".join(sorted(missing)))
        cols = {r["name"] for r in self.con.execute("PRAGMA table_info(stock)")}
        self.has_location = "location" in cols
        self.has_history = self.con.execute(
            "SELECT 1 FROM sqlite_master WHERE name='stock_history'").fetchone() is not None

    # --- writing -----------------------------------------------------------

    def backup(self):
        """Copy the database to backups/<name>-<timestamp>.sqlite, once per
        opened database (before its first change)."""
        if self.backup_path:
            return
        folder = os.path.join(os.path.dirname(self.path), "backups")
        os.makedirs(folder, exist_ok=True)
        stem = os.path.splitext(os.path.basename(self.path))[0]
        dest = os.path.join(folder, f"{stem}-{datetime.now():%Y%m%d-%H%M%S}.sqlite")
        out = sqlite3.connect(dest)
        self.con.backup(out)
        out.close()
        self.backup_path = dest
        if self.on_backup:
            self.on_backup(dest)

    @contextmanager
    def write(self):
        """Transaction: commit on success, roll back on any exception."""
        self.backup()
        self.con.execute("BEGIN IMMEDIATE")
        try:
            yield self.con
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        self.con.execute("COMMIT")

    def needs_migration(self):
        return not (self.has_location and self.has_history)

    def migrate(self):
        """Additive schema changes only; KiCad and bomer2.py are unaffected."""
        with self.write() as c:
            if not self.has_location:
                c.execute("ALTER TABLE stock ADD COLUMN location TEXT")
            if not self.has_history:
                c.execute("""CREATE TABLE stock_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dev_id integer NOT NULL,
                    timestamp TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                    delta integer NOT NULL,
                    quantity_after integer NOT NULL,
                    reason TEXT,
                    comment TEXT)""")
                c.execute("CREATE INDEX stock_history_dev ON stock_history (dev_id)")
        self.has_location = self.has_history = True

    # --- categories --------------------------------------------------------

    def categories(self):
        return self.con.execute(
            "SELECT id, parent, name FROM category ORDER BY name").fetchall()

    def category(self, cid):
        return self.con.execute("SELECT * FROM category WHERE id = ?",
                                (cid,)).fetchone()

    def _check_category_name(self, parent, name, exclude=None):
        name = (name or "").strip()
        if not name:
            raise ValueError("The name is empty.")
        if self.con.execute(
                """SELECT 1 FROM category WHERE parent = ? AND id != ?
                   AND id != ? AND name = ? COLLATE NOCASE""",
                (parent, parent, -1 if exclude is None else exclude,
                 name)).fetchone():
            raise ValueError(f"A category named '{name}' already exists there.")
        return name

    def add_category(self, parent, name, comment=None):
        name = self._check_category_name(parent, name)
        with self.write() as c:
            return c.execute(
                "INSERT INTO category (parent, name, comment) VALUES (?, ?, ?)",
                (parent, name, comment or None)).lastrowid

    def rename_category(self, cid, name):
        cat = self.category(cid)
        name = self._check_category_name(cat["parent"], name, exclude=cid)
        with self.write() as c:
            c.execute("UPDATE category SET name = ? WHERE id = ?", (name, cid))

    def set_category_comment(self, cid, comment):
        with self.write() as c:
            c.execute("UPDATE category SET comment = ? WHERE id = ?",
                      (comment or None, cid))

    def delete_category(self, cid):
        children = self.con.execute(
            "SELECT COUNT(*) FROM category WHERE parent = ? AND id != ?",
            (cid, cid)).fetchone()[0]
        devices = self.con.execute(
            "SELECT COUNT(*) FROM device WHERE category = ?", (cid,)).fetchone()[0]
        if children or devices:
            raise ValueError(f"The category still has {children} subcategories "
                             f"and {devices} components.")
        with self.write() as c:
            c.execute("DELETE FROM category WHERE id = ?", (cid,))

    # --- stock -------------------------------------------------------------

    def _apply_stock(self, c, dev_id, delta, reason, comment):
        """Inside a write() transaction: add `delta` and log it."""
        row = c.execute("SELECT quantity FROM stock WHERE dev_id = ?",
                        (dev_id,)).fetchone()
        if row is None:
            c.execute("INSERT INTO stock (dev_id, quantity, warn) VALUES (?, 0, 0)",
                      (dev_id,))
        qty = (row["quantity"] or 0 if row else 0) + delta
        if qty < 0:
            raise ValueError(f"#{dev_id}: stock would become negative ({qty}).")
        c.execute("UPDATE stock SET quantity = ? WHERE dev_id = ?", (qty, dev_id))
        c.execute("""INSERT INTO stock_history
                     (dev_id, delta, quantity_after, reason, comment)
                     VALUES (?, ?, ?, ?, ?)""",
                  (dev_id, delta, qty, reason, comment or None))
        return qty

    def change_stock(self, dev_id, delta, reason, comment=""):
        """Add `delta` (negative to consume) and log it. Returns new quantity."""
        with self.write() as c:
            return self._apply_stock(c, dev_id, delta, reason, comment)

    def change_stock_many(self, changes, reason, comment=""):
        """[(dev_id, delta), …] in one transaction: all or nothing."""
        with self.write() as c:
            for dev_id, delta in changes:
                self._apply_stock(c, dev_id, delta, reason, comment)

    def last_undoable(self, dev_id):
        """The newest history entry of `dev_id` that is not an undo and has
        not been undone yet, or None. Repeated undos walk back in time."""
        rows = self.con.execute(
            "SELECT * FROM stock_history WHERE dev_id = ? ORDER BY id DESC",
            (dev_id,)).fetchall()
        undone = set()
        for r in rows:
            if r["reason"] == "undo" and (r["comment"] or "").startswith(UNDO_PREFIX):
                ref = r["comment"][len(UNDO_PREFIX):].split()[0]
                if ref.isdigit():
                    undone.add(int(ref))
        for r in rows:
            if r["reason"] != "undo" and r["id"] not in undone:
                return r
        return None

    def undo_last(self, dev_id):
        """Reverse the last change with a compensating entry.
        Returns (undone history row, new quantity)."""
        r = self.last_undoable(dev_id)
        if r is None:
            raise ValueError("There is no stock change to undo.")
        with self.write() as c:
            qty = self._apply_stock(c, dev_id, -r["delta"], "undo",
                                    f"{UNDO_PREFIX}{r['id']} ({r['reason']})")
        return r, qty

    def history_log(self, date_from, date_to, reason="", text=""):
        """All stock changes between two ISO dates (inclusive), newest first."""
        if not self.has_history:
            return []
        sql = """SELECT h.id, h.timestamp, h.dev_id, d.value, d.value2, h.delta,
                        h.quantity_after, h.reason, h.comment
                 FROM stock_history h LEFT JOIN device d ON d.id = h.dev_id
                 WHERE date(h.timestamp) BETWEEN ? AND ?"""
        args = [date_from, date_to]
        if reason:
            sql += " AND h.reason = ?"
            args.append(reason)
        for word in text.split():
            sql += """ AND (d.value LIKE ? OR d.value2 LIKE ? OR h.comment LIKE ?
                            OR CAST(h.dev_id AS TEXT) = ?)"""
            args += [f"%{word}%"] * 3 + [word]
        return self.con.execute(sql + " ORDER BY h.id DESC", args).fetchall()

    def history_reasons(self):
        if not self.has_history:
            return []
        return [r[0] for r in self.con.execute(
            "SELECT DISTINCT reason FROM stock_history WHERE reason IS NOT NULL "
            "ORDER BY reason")]

    def low_stock(self):
        """Components at or below their warning level, one row per source."""
        loc = "s.location" if self.has_location else "NULL"
        return self.con.execute(f"""
            SELECT d.id, d.value, d.value2, c.name AS category, s.quantity,
                   s.warn, {loc} AS location, sp.name AS supplier,
                   so.ordering_code, so.uprice, so.ppu
            FROM stock s
            JOIN device d ON d.id = s.dev_id
            LEFT JOIN category c ON c.id = d.category
            LEFT JOIN source so ON so.dev_id = d.id
            LEFT JOIN supplier sp ON sp.id = so.sup_id
            WHERE s.quantity <= s.warn
            ORDER BY c.name, d.value, sp.name""").fetchall()

    def set_stock_settings(self, dev_id, warn, location):
        location = (location or "").strip() or None
        with self.write() as c:
            if c.execute("SELECT 1 FROM stock WHERE dev_id = ?", (dev_id,)).fetchone():
                c.execute("UPDATE stock SET warn = ?, location = ? WHERE dev_id = ?",
                          (warn, location, dev_id))
            else:
                c.execute("""INSERT INTO stock (dev_id, quantity, warn, location)
                             VALUES (?, 0, ?, ?)""", (dev_id, warn, location))

    def locations(self):
        if not self.has_location:
            return []
        return [r[0] for r in self.con.execute(
            """SELECT DISTINCT location FROM stock WHERE location IS NOT NULL
               AND location != '' ORDER BY location""")]

    def suppliers(self):
        return self.con.execute("SELECT id, name FROM supplier ORDER BY name").fetchall()

    # --- suppliers ---------------------------------------------------------

    def supplier_rows(self):
        return self.con.execute(
            "SELECT * FROM supplier ORDER BY name COLLATE NOCASE").fetchall()

    def supplier_by_names(self, names):
        """First supplier whose name contains one of `names` (lower case)."""
        for r in self.suppliers():
            if any(n in (r["name"] or "").lower() for n in names):
                return r["id"]
        return None

    def save_supplier(self, sup_id, values):
        name = (values.get("name") or "").strip()
        if not name:
            raise ValueError("The name is empty.")
        dup = self.con.execute(
            "SELECT id FROM supplier WHERE name = ? COLLATE NOCASE AND id != ?",
            (name, sup_id or -1)).fetchone()
        if dup:
            raise ValueError(f"A supplier named '{name}' already exists.")
        cols = [k for k, _ in SUPPLIER_FIELDS]
        vals = [values.get(k) if values.get(k) not in ("",) else None for k in cols]
        with self.write() as c:
            if sup_id is None:
                return c.execute(
                    f"INSERT INTO supplier ({', '.join(cols)}) "
                    f"VALUES ({', '.join('?' * len(cols))})", vals).lastrowid
            c.execute(f"UPDATE supplier SET {', '.join(f'{k} = ?' for k in cols)} "
                      "WHERE id = ?", vals + [sup_id])
            return sup_id

    def delete_supplier(self, sup_id):
        used = self.con.execute("SELECT COUNT(*) FROM source WHERE sup_id = ?",
                                (sup_id,)).fetchone()[0]
        if used:
            raise ValueError(f"The supplier is used by {used} source(s) of "
                             "components. Remove those first.")
        with self.write() as c:
            c.execute("DELETE FROM supplier WHERE id = ?", (sup_id,))

    # --- components --------------------------------------------------------

    def cad_variants(self, dev_id):
        return self.con.execute(
            "SELECT COUNT(*) FROM cad_data WHERE dev_id = ? AND cad_tool = ?",
            (dev_id, KICAD_TOOL)).fetchone()[0]

    def source_rows(self, dev_id):
        return self.con.execute(
            "SELECT sup_id, ordering_code, uprice, ppu, comment FROM source "
            "WHERE dev_id = ? ORDER BY rowid", (dev_id,)).fetchall()

    def similar_devices(self, value, footprint, exclude=None):
        """Ids of other components with the same value and footprint."""
        return [r[0] for r in self.con.execute(
            f"""SELECT d.id FROM device d
                LEFT JOIN cad_data a ON a.dev_id = d.id AND a.cad_tool = {KICAD_TOOL}
                WHERE d.value = ? COLLATE NOCASE AND COALESCE(a.footprint, '') = ?
                AND d.id != ?""", (value, footprint or "", exclude or -1))]

    def save_component(self, dev_id, category, device, cad, sources, stock=None,
                       stock_reason="initial", stock_comment=""):
        """Insert (dev_id None) or update a component in one transaction.

        device:  {DEVICE_FIELDS column: text}
        cad:     {"id": cad_data id or None, CAD_FIELDS columns, "No BOM",
                  "Schematic Only"}; a new cad_data row (variant 1) is only
                 created if a symbol or footprint is given
        sources: [{"sup_id", "ordering_code", "uprice", "ppu", "comment"}],
                 replaces all sources of the component
        stock:   {"quantity", "warn", "location"} or None to leave it alone;
                 a quantity change is logged in stock_history
        Returns the component id.
        """
        def clean(v):
            if isinstance(v, str):
                v = v.strip()
                return v or None
            return v

        cols = ["category"] + DEVICE_FIELDS
        vals = [category] + [clean(device.get(k)) for k in DEVICE_FIELDS]
        ccols = [k for k, _ in CAD_FIELDS]
        cvals = ([clean(cad.get(k)) for k in ccols]
                 + [int(bool(cad.get("No BOM"))), int(bool(cad.get("Schematic Only")))])
        ccols = ccols + ["No BOM", "Schematic Only"]
        q = [f'"{k}"' for k in ccols]
        with self.write() as c:
            new = dev_id is None
            if new:
                dev_id = c.execute(
                    f"INSERT INTO device ({', '.join(cols)}) "
                    f"VALUES ({', '.join('?' * len(cols))})", vals).lastrowid
            else:
                c.execute(f"UPDATE device SET {', '.join(f'{k} = ?' for k in cols)} "
                          "WHERE id = ?", vals + [dev_id])
            if cad.get("id"):
                c.execute(f"UPDATE cad_data SET {', '.join(f'{k} = ?' for k in q)} "
                          "WHERE id = ?", cvals + [cad["id"]])
            elif clean(cad.get("symbol")) or clean(cad.get("footprint")):
                c.execute(f"""INSERT INTO cad_data (dev_id, variant, cad_tool, {', '.join(q)})
                              VALUES (?, 1, ?, {', '.join('?' * len(q))})""",
                          [dev_id, KICAD_TOOL] + cvals)
            c.execute("DELETE FROM source WHERE dev_id = ?", (dev_id,))
            for src in sources:
                c.execute("""INSERT INTO source (sup_id, dev_id, uprice, ppu,
                                                 ordering_code, comment)
                             VALUES (?, ?, ?, ?, ?, ?)""",
                          (src["sup_id"], dev_id, src.get("uprice") or 0,
                           src.get("ppu") or 1, clean(src.get("ordering_code")),
                           clean(src.get("comment"))))
            if stock is not None and not self.needs_migration():
                location = clean(stock.get("location"))
                row = c.execute("SELECT quantity FROM stock WHERE dev_id = ?",
                                (dev_id,)).fetchone()
                if row is None and (stock["quantity"] or stock["warn"] or location):
                    c.execute("""INSERT INTO stock (dev_id, quantity, warn, location)
                                 VALUES (?, 0, ?, ?)""", (dev_id, stock["warn"], location))
                    row = {"quantity": 0}
                elif row is not None:
                    c.execute("UPDATE stock SET warn = ?, location = ? WHERE dev_id = ?",
                              (stock["warn"], location, dev_id))
                if row is not None:
                    delta = stock["quantity"] - (row["quantity"] or 0)
                    if delta:
                        self._apply_stock(c, dev_id, delta,
                                          stock_reason if new else "correction",
                                          stock_comment)
        return dev_id

    def component_usage(self, dev_id):
        """Row counts that delete_component() would remove."""
        def count(sql):
            return self.con.execute(sql, (dev_id,)).fetchone()[0]
        out = {"CAD data": count("SELECT COUNT(*) FROM cad_data WHERE dev_id = ?"),
               "sources": count("SELECT COUNT(*) FROM source WHERE dev_id = ?"),
               "stock": count("SELECT COUNT(*) FROM stock WHERE dev_id = ?")}
        if self.has_history:
            out["stock history"] = count(
                "SELECT COUNT(*) FROM stock_history WHERE dev_id = ?")
        return out

    def delete_component(self, dev_id):
        with self.write() as c:
            if self.has_history:
                c.execute("DELETE FROM stock_history WHERE dev_id = ?", (dev_id,))
            for table in ("stock", "source", "cad_data"):
                c.execute(f"DELETE FROM {table} WHERE dev_id = ?", (dev_id,))
            c.execute("DELETE FROM device WHERE id = ?", (dev_id,))

    def find_by_distributor(self, code, sup_id=None):
        """Component ids matching a DistributorCode, best matches first."""
        found = []

        def add(sql, args):
            for r in self.con.execute(sql, args):
                if r[0] not in found:
                    found.append(r[0])

        pn, mpn = code.part_number.strip(), code.mpn.strip()
        if pn and sup_id is not None:
            add("""SELECT dev_id FROM source WHERE sup_id = ?
                   AND ordering_code = ? COLLATE NOCASE""", (sup_id, pn))
        if pn:
            add("SELECT dev_id FROM source WHERE ordering_code = ? COLLATE NOCASE", (pn,))
        if mpn:
            # Mouser style ordering codes: "<prefix>-<MPN>"
            add("""SELECT dev_id FROM source WHERE ordering_code = ? COLLATE NOCASE
                   OR ordering_code LIKE ?""", (mpn, f"%-{mpn}"))
            add("SELECT id FROM device WHERE value = ? COLLATE NOCASE", (mpn,))
        return found

    def search(self, text="", category_ids=None, stock_filter="all"):
        loc = "s.location" if self.has_location else "NULL"
        sql = f"""
            SELECT d.id, d.value, d.value2, d.value3, c.name AS category,
                   (SELECT a.footprint FROM cad_data a
                     WHERE a.dev_id = d.id AND a.cad_tool = {KICAD_TOOL}
                     LIMIT 1) AS footprint,
                   s.quantity, s.warn, {loc} AS location
            FROM device d
            LEFT JOIN category c ON c.id = d.category
            LEFT JOIN stock s ON s.dev_id = d.id
            WHERE 1 = 1
        """
        args = []
        for word in text.split():
            # "id:23" matches exactly that id (used for scanned labels).
            if word.lower().startswith("id:") and word[3:].isdigit():
                sql += " AND d.id = ?"
                args.append(int(word[3:]))
                continue
            like = f"%{word}%"
            sql += f"""
              AND (d.value LIKE ? OR d.value2 LIKE ? OR d.value3 LIKE ?
                   OR d.type LIKE ? OR d.description LIKE ? OR c.name LIKE ?
                   OR {loc} LIKE ? OR CAST(d.id AS TEXT) = ?
                   OR EXISTS (SELECT 1 FROM source so WHERE so.dev_id = d.id
                              AND so.ordering_code LIKE ?)
                   OR EXISTS (SELECT 1 FROM cad_data a WHERE a.dev_id = d.id
                              AND (a."Keywords" LIKE ? OR a.footprint LIKE ?)))"""
            args += [like] * 7 + [word] + [like] * 3
        if category_ids is not None:
            sql += f" AND d.category IN ({','.join('?' * len(category_ids))})"
            args += list(category_ids)
        if stock_filter == "in stock":
            sql += " AND s.quantity > 0"
        elif stock_filter == "out of stock":
            sql += " AND COALESCE(s.quantity, 0) <= 0"
        elif stock_filter == "low stock":
            sql += " AND s.quantity IS NOT NULL AND s.quantity <= s.warn"
        sql += " ORDER BY c.name, d.value LIMIT 2000"
        return self.con.execute(sql, args).fetchall()

    def device(self, dev_id):
        return self.con.execute(
            """SELECT d.*, c.name AS category_name FROM device d
               LEFT JOIN category c ON c.id = d.category WHERE d.id = ?""",
            (dev_id,)).fetchone()

    def cad(self, dev_id):
        """The KiCad row of a component (lowest variant), or None."""
        return self.con.execute(
            "SELECT * FROM cad_data WHERE dev_id = ? AND cad_tool = ? "
            "ORDER BY variant, id LIMIT 1", (dev_id, KICAD_TOOL)).fetchone()

    def sources(self, dev_id):
        return self.con.execute(
            """SELECT sp.name AS supplier, so.ordering_code, so.uprice, so.ppu,
                      so.comment
               FROM source so LEFT JOIN supplier sp ON sp.id = so.sup_id
               WHERE so.dev_id = ?""", (dev_id,)).fetchall()

    def stock(self, dev_id):
        loc = "location" if self.has_location else "NULL AS location"
        return self.con.execute(
            f"SELECT quantity, warn, {loc} FROM stock WHERE dev_id = ?",
            (dev_id,)).fetchone()

    def history(self, dev_id):
        if not self.has_history:
            return []
        return self.con.execute(
            """SELECT id, timestamp, delta, quantity_after, reason, comment
               FROM stock_history WHERE dev_id = ?
               ORDER BY timestamp DESC, id DESC""",
            (dev_id,)).fetchall()


# --------------------------------------------------------------------------
# Label rendering
# --------------------------------------------------------------------------

# QR error correction levels: share of the code that may be damaged.
QR_EC_LEVELS = [("L", "L – 7 %"), ("M", "M – 15 %"), ("Q", "Q – 25 %"),
                ("H", "H – 30 %")]


def code_matrix(payload, ec="M", border=0):
    """Module matrix (True = black) of the 2D code, including `border`
    modules of quiet zone. Uses the `qrcode` package if available, otherwise
    a clearly fake placeholder pattern."""
    try:
        import qrcode
        qr = qrcode.QRCode(border=border, error_correction=getattr(
            qrcode.constants, f"ERROR_CORRECT_{ec}"))
        qr.add_data(payload)
        qr.make(fit=True)
        return qr.get_matrix()
    except ImportError:
        n = 14
        bits = bin(int(hashlib.sha256(payload.encode()).hexdigest(), 16))[2:]
        bits = (bits * 2)[: n * n]
        matrix = [[bits[r * n + c] == "1" for c in range(n)] for r in range(n)]
        for i in range(n):  # Data Matrix style finder: solid L + dashed edges
            matrix[n - 1][i] = matrix[i][0] = True
            matrix[0][i] = matrix[i][n - 1] = i % 2 == 0
        pad = [[False] * (n + 2 * border)]
        return (pad * border + [[False] * border + r + [False] * border
                                for r in matrix] + pad * border)


def draw_code(p, x, y, size, payload, ec="M", border=0):
    """Draw the code in a `size` × `size` square. Every module gets the same
    whole number of pixels, which prints sharper than fractional sizes; the
    code is centred in the square. Returns (modules, pixels per module)."""
    matrix = code_matrix(payload, ec, border)
    n = len(matrix)
    cell = max(1, size // n)
    off = (size - cell * n) // 2
    for r, row in enumerate(matrix):
        for c, on in enumerate(row):
            if on:
                p.fillRect(x + off + c * cell, y + off + r * cell, cell, cell, Qt.black)
    return n, cell


def fit_font(p, text, width, size, minimum=7, bold=True):
    """Largest font from `size` down to `minimum` points in which `text` fits
    `width` pixels (the caller still elides if even `minimum` is too wide)."""
    for pt in range(size, minimum - 1, -1):
        font = QFont("Sans", pt, QFont.Bold if bold else QFont.Normal)
        p.setFont(font)
        if p.fontMetrics().horizontalAdvance(text) <= width:
            return font
    return font


def render_label(dev, footprint, location, options):
    """Render a label. Size from options["size_px"] (default 384 × 240).

    Normal layout (labels at least 180 px = 22 mm high):

        +---------+------------------+
        |         | #ID  (large)     |
        |   QR    | value2 value3    |
        |  code   | footprint, ...   |
        |         | location (bold)  |
        +---------+------------------+
        | VALUE (full width)         |
        +----------------------------+

    Compact layout (lower labels): the QR code uses the full height and the
    value moves into the right column, below the id.
    """
    W, H = options.get("size_px", (LABEL_W, LABEL_H))
    img = QImage(W, H, QImage.Format_RGB32)
    img.fill(Qt.white)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, False)
    p.setPen(Qt.black)
    compact = H < 180
    margin, gap = (8, 6) if not compact else (4, 4)
    fs = options["font_size"] if not compact else max(options["font_size"] - 4, 8)
    full_w = W - 2 * margin
    value = str(dev["value"] or "")

    if compact:
        code = H - 2 * margin
    else:
        # Bottom row: the value, as large as fits the full width.
        p.setFont(QFont("Sans", fs, QFont.Bold))
        value_h = p.fontMetrics().height()
        p.setFont(fit_font(p, value, full_w, fs))
        p.drawText(margin, H - margin - value_h, full_w, value_h,
                   Qt.AlignLeft | Qt.AlignVCenter,
                   p.fontMetrics().elidedText(value, Qt.ElideRight, full_w))
        code = H - 2 * margin - value_h - gap

    # QR code, top left.
    n, cell = draw_code(p, margin, margin, code, f"{LABEL_PREFIX}{dev['id']}",
                        options.get("qr_ec", "H"), options.get("qr_border", 0))
    border = options.get("qr_border", 0)
    img.setText("qr_info", f"{n - 2 * border} × {n - 2 * border} modules, "
                f"{cell} px = {cell / 8:.2f} mm per module")

    # Right column: ID, (value), details, location at the bottom.
    tx = margin + code + gap * 2
    tw = W - tx - margin
    bottom = margin + code
    id_text = f"#{dev['id']}"
    p.setFont(fit_font(p, id_text, tw, fs + (10 if not compact else 4)))
    h = p.fontMetrics().height()
    p.drawText(tx, margin, tw, h, Qt.AlignLeft | Qt.AlignTop, id_text)
    ty = margin + h
    if compact and value:
        p.setFont(fit_font(p, value, tw, fs))
        h = p.fontMetrics().height()
        p.drawText(tx, ty, tw, h, Qt.AlignLeft | Qt.AlignVCenter,
                   p.fontMetrics().elidedText(value, Qt.ElideRight, tw))
        ty += h

    loc_font = QFont("Sans", max(fs - 4, 7), QFont.Bold)
    p.setFont(loc_font)
    loc_h = p.fontMetrics().height() if options["location"] and location else 0
    small = QFont("Sans", max(fs - 6, 6))

    def line(text):
        nonlocal ty
        if not text:
            return
        p.setFont(small)
        h = p.fontMetrics().height()
        if ty + h > bottom - loc_h:  # no room left: skip
            return
        p.drawText(tx, ty, tw, h, Qt.AlignLeft | Qt.AlignVCenter,
                   p.fontMetrics().elidedText(str(text), Qt.ElideRight, tw))
        ty += h

    if options["value2"]:
        line(" ".join(v for v in (dev["value2"], dev["value3"]) if v))
    if options["footprint"]:
        if footprint and options.get("short_footprint"):
            footprint = footprint.split(":", 1)[-1]
        line(footprint)
    if options["category"]:
        line(dev["category_name"])
    if options["description"]:
        line(dev["description"])
    if loc_h:
        p.setFont(loc_font)
        p.drawText(tx, bottom - loc_h, tw, loc_h, Qt.AlignLeft | Qt.AlignVCenter,
                   p.fontMetrics().elidedText(location, Qt.ElideRight, tw))
    p.end()
    return img


def write_labels_pdf(path, images):
    """One label per page, page size = label size (8 px per mm)."""
    writer = QPdfWriter(path)
    first = images[0]
    writer.setPageSize(QPageSize(QSizeF(first.width() / 8, first.height() / 8),
                                 QPageSize.Millimeter))
    writer.setPageMargins(QMarginsF(0, 0, 0, 0))
    writer.setResolution(203)
    p = QPainter(writer)
    for i, img in enumerate(images):
        if i:
            writer.newPage()
        p.drawImage(QRectF(0, 0, writer.width(), writer.height()), img)
    p.end()


# --------------------------------------------------------------------------
# Dialogs
# --------------------------------------------------------------------------

def fill_category_tree(tree, categories):
    """Fill `tree` from category rows using `category.parent`.

    Returns ({id: item}, {parent_id: [child ids]}). A category that is its
    own parent (`root`) or has an unknown parent becomes a top-level item.
    """
    items, children = {}, {}
    for c in categories:
        items[c["id"]] = QTreeWidgetItem([c["name"] or f"#{c['id']}"])
        items[c["id"]].setData(0, Qt.UserRole, c["id"])
    for c in categories:
        if c["id"] == c["parent"] or c["parent"] not in items:
            tree.addTopLevelItem(items[c["id"]])
        else:
            items[c["parent"]].addChild(items[c["id"]])
            children.setdefault(c["parent"], []).append(c["id"])
    tree.sortItems(0, Qt.AscendingOrder)
    for i in range(tree.topLevelItemCount()):
        tree.topLevelItem(i).setExpanded(True)
    return items, children


def ask_new_category(widget, db, parent_id):
    """Ask for a name and add a subcategory. Returns its id or None."""
    parent = db.category(parent_id)
    if parent is None:
        return None
    name, ok = QInputDialog.getText(
        widget, "New category", f"New subcategory under '{parent['name']}':")
    if not ok:
        return None
    try:
        return db.add_category(parent_id, name)
    except (ValueError, sqlite3.Error) as e:
        QMessageBox.warning(widget, "New category", str(e))
        return None


class CategoryPicker(QWidget):
    """Category tree with a filter box, the full path of the selection and a
    button to add a subcategory."""

    def __init__(self, db, selected=None):
        super().__init__()
        self.db = db
        self.changed = False  # True once a category was added here
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        self.filter = QLineEdit(placeholderText="Filter categories…",
                                clearButtonEnabled=True)
        self.filter.textChanged.connect(self._apply_filter)
        v.addWidget(self.filter)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.currentItemChanged.connect(self._update_path)
        v.addWidget(self.tree, 1)
        self.path = QLabel(wordWrap=True)
        v.addWidget(self.path)
        add = QPushButton("New subcategory…")
        add.setToolTip("Add a category under the selected one")
        add.clicked.connect(self._add)
        v.addWidget(add)
        self._load(selected)

    def _load(self, selected):
        self.tree.clear()
        self.items, _ = fill_category_tree(self.tree, self.db.categories())
        if selected in self.items:
            self.tree.setCurrentItem(self.items[selected])
            self.tree.scrollToItem(self.items[selected])
        self._apply_filter(self.filter.text())
        self._update_path()

    def _add(self):
        parent = self.category_id()
        if parent is None:
            QMessageBox.information(self, "New category",
                                    "Select the parent category first.")
            return
        new_id = ask_new_category(self, self.db, parent)
        if new_id is not None:
            self.changed = True
            self.filter.clear()
            self._load(new_id)

    def category_id(self):
        item = self.tree.currentItem()
        return item.data(0, Qt.UserRole) if item else None

    def _update_path(self, *_):
        parts, item = [], self.tree.currentItem()
        while item is not None:
            parts.insert(0, item.text(0))
            item = item.parent()
        self.path.setText("<b>" + " / ".join(parts) + "</b>" if parts
                          else "<span style='color:red'>No category selected</span>")

    def _apply_filter(self, text):
        text = text.strip().lower()

        def walk(item, parent_matches):
            matches = parent_matches or text in item.text(0).lower()
            child_visible = False
            for i in range(item.childCount()):
                child_visible |= walk(item.child(i), matches)
            item.setHidden(not (matches or child_visible))
            if text and child_visible:
                item.setExpanded(True)
            return matches or child_visible

        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i), not text)


class StockDialog(QDialog):
    """Receive or consume stock. Designed for scanner workflow: the quantity
    field has focus, Enter confirms."""

    def __init__(self, parent, dev, current, receive=True, qty=None, comment=""):
        super().__init__(parent)
        self.setWindowTitle("Receive stock" if receive else "Consume stock")
        form = QFormLayout(self)
        form.addRow("Component:", QLabel(
            f"<b>#{dev['id']}</b> {dev['value'] or ''} {dev['value2'] or ''}"))
        form.addRow("Current quantity:", QLabel(str(current)))
        self.qty = QSpinBox(maximum=1_000_000 if receive else current, minimum=1)
        form.addRow("Quantity:", self.qty)
        self.reason = QComboBox()
        self.reason.addItems(["received", "correction"] if receive
                             else ["consumed", "BOM", "correction"])
        form.addRow("Reason:", self.reason)
        self.comment = QLineEdit(placeholderText=(
            "order number" if receive else "project / board"))
        form.addRow("Comment:", self.comment)
        if qty:
            self.qty.setValue(qty)
        if comment:
            self.comment.setText(comment)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self.qty.setFocus()
        self.qty.selectAll()


class StockSettingsDialog(QDialog):
    """Warning level and storage location of one component."""

    def __init__(self, parent, db, dev, stock):
        super().__init__(parent)
        self.setWindowTitle("Stock settings")
        form = QFormLayout(self)
        form.addRow("Component:", QLabel(
            f"<b>#{dev['id']}</b> {dev['value'] or ''} {dev['value2'] or ''}"))
        self.warn = QSpinBox(maximum=1_000_000,
                             value=(stock["warn"] or 0) if stock else 0)
        form.addRow("Warn at or below:", self.warn)
        self.location = QLineEdit((stock["location"] or "") if stock else "",
                                  placeholderText="Cabinet A / Drawer 12")
        completer = QCompleter(db.locations(), self)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        self.location.setCompleter(completer)
        self.location.setMinimumWidth(260)
        form.addRow("Location:", self.location)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)


class ComponentDialog(QDialog):
    """Add, edit or duplicate a component: device fields, KiCad data (the
    lowest cad_data variant for KiCad), sources and stock. Saves in one
    transaction; `saved_id` holds the component id afterwards.

    mode: "new", "edit" or "duplicate" (dev_id is the original).
    prefill (new components): {"value", "comment", "sources": [...],
    "quantity", "stock_comment"}.
    """

    def __init__(self, parent, db, dev_id=None, mode="new", category_id=None,
                 prefill=None):
        super().__init__(parent)
        self.db, self.mode, self.saved_id = db, mode, None
        self.dev_id = dev_id if mode == "edit" else None
        prefill = prefill or {}
        dev = db.device(dev_id) if dev_id is not None else None
        cad = db.cad(dev_id) if dev_id is not None else None
        stock = db.stock(dev_id) if dev_id is not None else None
        self.cad_id = cad["id"] if (cad and mode == "edit") else None
        self.setWindowTitle({"new": "New component",
                             "edit": f"Edit component #{dev_id}",
                             "duplicate": f"Duplicate component #{dev_id}"}[mode])
        self.resize(900, 580)
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        lay.addWidget(tabs)

        def val(row, key):
            return "" if row is None or row[key] is None else str(row[key])

        # --- Device
        w = QWidget()
        h = QHBoxLayout(w)
        box = QGroupBox("Category")
        self.category = CategoryPicker(db, dev["category"] if dev else category_id)
        QVBoxLayout(box).addWidget(self.category)
        h.addWidget(box, 2)
        f = QFormLayout()
        h.addLayout(f, 3)
        self.dev_edits = {}
        labels = {"value": "Value *", "value2": "Value2", "value3": "Value3",
                  "technology": "Technology", "type": "Type",
                  "description": "Description", "documentation": "Datasheet URL",
                  "comment": "Comment"}
        for key in DEVICE_FIELDS:
            e = QLineEdit(val(dev, key) or prefill.get(key, ""))
            self.dev_edits[key] = e
            f.addRow(labels[key] + ":", e)
        tabs.addTab(w, "Device")

        # --- KiCad
        w = QWidget()
        f = QFormLayout(w)
        self.cad_edits = {}
        for key, label in CAD_FIELDS:
            e = QLineEdit(val(cad, key))
            self.cad_edits[key] = e
            f.addRow(label + ":", e)
        self.nobom = QCheckBox("No BOM", checked=bool(cad and cad["No BOM"]))
        self.schonly = QCheckBox("Schematic only", checked=bool(cad and cad["Schematic Only"]))
        f.addRow(self.nobom)
        f.addRow(self.schonly)
        note = ("Without a symbol and footprint no KiCad row is created, and the "
                "component does not appear in the KiCad chooser.")
        if dev_id is not None and db.cad_variants(dev_id) > 1:
            note += (f"<br>This component has {db.cad_variants(dev_id)} KiCad variants; "
                     "this tab edits variant 1, the others are kept.")
        f.addRow(QLabel(f"<small>{note}</small>", wordWrap=True))
        tabs.addTab(w, "KiCad")

        # --- Sources
        w = QWidget()
        v = QVBoxLayout(w)
        self.suppliers = db.suppliers()
        self.sources = QTableWidget(0, 5)
        self.sources.setHorizontalHeaderLabels(
            ["Supplier", "Ordering code", "Price", "For (pcs)", "Comment"])
        self.sources.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        srcs = [dict(r) for r in db.source_rows(dev_id)] if dev_id is not None else []
        if mode == "duplicate":
            for src in srcs:
                src["ordering_code"] = ""  # a different part has a different code
        srcs += prefill.get("sources", [])
        for src in srcs:
            self._add_source(src)
        v.addWidget(self.sources)
        hb = QHBoxLayout()
        add = QPushButton("Add source")
        add.clicked.connect(lambda: self._add_source({}))
        rm = QPushButton("Remove source")
        rm.clicked.connect(lambda: self.sources.removeRow(self.sources.currentRow()))
        hb.addWidget(add)
        hb.addWidget(rm)
        hb.addStretch()
        v.addLayout(hb)
        tabs.addTab(w, "Sources")

        # --- Stock
        w = QWidget()
        f = QFormLayout(w)
        qty = int(val(stock, "quantity") or 0) if mode == "edit" else prefill.get("quantity", 0)
        self.qty = QSpinBox(maximum=1_000_000, value=qty)
        f.addRow("Quantity:", self.qty)
        self.warn = QSpinBox(maximum=1_000_000, value=int(val(stock, "warn") or 0))
        f.addRow("Warn at or below:", self.warn)
        self.location = QLineEdit(val(stock, "location") if mode == "edit" else "",
                                  placeholderText="Cabinet A / Drawer 12")
        completer = QCompleter(db.locations(), self)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        self.location.setCompleter(completer)
        f.addRow("Location:", self.location)
        self.stock_comment = prefill.get("stock_comment", "")
        if db.needs_migration():
            w.setEnabled(False)
            f.addRow(QLabel("<small>The database is not upgraded, so stock cannot "
                            "be changed.</small>", wordWrap=True))
        elif mode == "edit":
            f.addRow(QLabel("<small>A quantity change here is logged as a "
                            "correction.</small>", wordWrap=True))
        tabs.addTab(w, "Stock")

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.dev_edits["value"].setFocus()

    def _add_source(self, src):
        t = self.sources
        r = t.rowCount()
        t.insertRow(r)
        combo = QComboBox()
        for sp in self.suppliers:
            combo.addItem(sp["name"], sp["id"])
        if src.get("sup_id") is not None:
            combo.setCurrentIndex(max(0, combo.findData(src["sup_id"])))
        t.setCellWidget(r, 0, combo)
        t.setItem(r, 1, QTableWidgetItem(src.get("ordering_code") or ""))
        price = QDoubleSpinBox(maximum=1e9, decimals=4, value=float(src.get("uprice") or 0))
        t.setCellWidget(r, 2, price)
        ppu = QSpinBox(minimum=1, maximum=1_000_000, value=int(src.get("ppu") or 1))
        t.setCellWidget(r, 3, ppu)
        t.setItem(r, 4, QTableWidgetItem(src.get("comment") or ""))

    def _source_values(self):
        t, out = self.sources, []
        for r in range(t.rowCount()):
            out.append({
                "sup_id": t.cellWidget(r, 0).currentData(),
                "ordering_code": t.item(r, 1).text() if t.item(r, 1) else "",
                "uprice": t.cellWidget(r, 2).value(),
                "ppu": t.cellWidget(r, 3).value(),
                "comment": t.item(r, 4).text() if t.item(r, 4) else "",
            })
        return out

    def accept(self):
        category = self.category.category_id()
        if category is None:
            QMessageBox.warning(self, "Component", "Select a category.")
            return
        value = self.dev_edits["value"].text().strip()
        if not value:
            QMessageBox.warning(self, "Component", "The value is empty.")
            self.dev_edits["value"].setFocus()
            return
        sources = self._source_values()
        if any(src["sup_id"] is None for src in sources):
            QMessageBox.warning(self, "Component", "Every source needs a supplier.")
            return
        footprint = self.cad_edits["footprint"].text().strip()
        same = self.db.similar_devices(value, footprint, self.dev_id)
        if same and QMessageBox.question(
                self, "Component",
                f"Component(s) {', '.join(f'#{i}' for i in same)} already have value "
                f"'{value}' and footprint '{footprint or '(none)'}'.\n\nSave anyway?"
                ) != QMessageBox.Yes:
            return
        device = {k: e.text() for k, e in self.dev_edits.items()}
        cad = {k: e.text() for k, e in self.cad_edits.items()}
        cad.update({"id": self.cad_id, "No BOM": self.nobom.isChecked(),
                    "Schematic Only": self.schonly.isChecked()})
        stock = {"quantity": self.qty.value(), "warn": self.warn.value(),
                 "location": self.location.text()}
        try:
            self.saved_id = self.db.save_component(
                self.dev_id, category, device, cad, sources, stock,
                stock_comment=self.stock_comment)
        except (ValueError, sqlite3.Error) as e:
            QMessageBox.critical(self, "Component", f"Saving failed:\n\n{e}")
            return
        super().accept()


class SupplierDialog(QDialog):
    """Add, edit and delete suppliers."""

    def __init__(self, parent, db):
        super().__init__(parent)
        self.db = db
        self.sup_id = None
        self.dirty = False
        self.setWindowTitle("Suppliers")
        self.resize(760, 480)
        lay = QHBoxLayout(self)
        left = QVBoxLayout()
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._select)
        left.addWidget(self.list, 1)
        hb = QHBoxLayout()
        new = QPushButton("New")
        new.clicked.connect(self._new)
        delete = QPushButton("Delete")
        delete.clicked.connect(self._delete)
        hb.addWidget(new)
        hb.addWidget(delete)
        left.addLayout(hb)
        lay.addLayout(left, 1)

        right = QVBoxLayout()
        form = QFormLayout()
        self.edits = {}
        for key, label in SUPPLIER_FIELDS:
            if key == "type":
                e = QSpinBox(minimum=0, maximum=99)
                e.setToolTip("Free number, for example 1 for an assembly service")
                e.valueChanged.connect(self._changed)
            elif key == "comment":
                e = QPlainTextEdit(maximumHeight=70)
                e.textChanged.connect(self._changed)
            else:
                e = QLineEdit()
                e.textEdited.connect(self._changed)
            self.edits[key] = e
            form.addRow(label + ":", e)
        right.addLayout(form)
        self.usage = QLabel()
        right.addWidget(self.usage)
        right.addStretch()
        hb = QHBoxLayout()
        self.save_b = QPushButton("Save")
        self.save_b.clicked.connect(self._save)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        hb.addStretch()
        hb.addWidget(self.save_b)
        hb.addWidget(close)
        right.addLayout(hb)
        lay.addLayout(right, 2)
        self._load()

    def _load(self, select=None):
        self.list.blockSignals(True)
        self.list.clear()
        for r in self.db.supplier_rows():
            it = QListWidgetItem(r["name"] or f"#{r['id']}")
            it.setData(Qt.UserRole, r["id"])
            self.list.addItem(it)
            if r["id"] == select:
                self.list.setCurrentItem(it)
        self.list.blockSignals(False)
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)
        else:
            self._show(self.list.currentItem().data(Qt.UserRole)
                       if self.list.currentItem() else None)

    def _changed(self, *_):
        self.dirty = True

    def _maybe_save(self):
        """Ask about unsaved changes. Returns False to stay on the record."""
        if not self.dirty:
            return True
        ans = QMessageBox.question(
            self, "Suppliers", "Save the changes to this supplier?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if ans == QMessageBox.Save:
            return self._save(reload=False)
        return ans == QMessageBox.Discard

    def _select(self, cur, prev):
        if prev is not None and not self._maybe_save():
            self.list.blockSignals(True)
            self.list.setCurrentItem(prev)
            self.list.blockSignals(False)
            return
        self._show(cur.data(Qt.UserRole) if cur else None)

    def _show(self, sup_id):
        self.sup_id = sup_id
        row = None
        if sup_id is not None:
            row = next((r for r in self.db.supplier_rows() if r["id"] == sup_id), None)
        for key, e in self.edits.items():
            v = row[key] if row is not None and row[key] is not None else ""
            if isinstance(e, QSpinBox):
                e.blockSignals(True)
                e.setValue(int(v or 0))
                e.blockSignals(False)
            elif isinstance(e, QPlainTextEdit):
                e.blockSignals(True)
                e.setPlainText(str(v))
                e.blockSignals(False)
            else:
                e.setText(str(v))
        used = 0
        if sup_id is not None:
            used = self.db.con.execute("SELECT COUNT(*) FROM source WHERE sup_id = ?",
                                       (sup_id,)).fetchone()[0]
        self.usage.setText(f"<small>{'New supplier' if sup_id is None else f'#{sup_id}'}"
                           f" · used by {used} source(s)</small>")
        self.dirty = False

    def _values(self):
        out = {}
        for key, e in self.edits.items():
            if isinstance(e, QSpinBox):
                out[key] = e.value()
            elif isinstance(e, QPlainTextEdit):
                out[key] = e.toPlainText().strip()
            else:
                out[key] = e.text().strip()
        return out

    def _new(self):
        if not self._maybe_save():
            return
        self.list.blockSignals(True)
        self.list.clearSelection()
        self.list.setCurrentItem(None)
        self.list.blockSignals(False)
        self._show(None)
        self.edits["name"].setFocus()

    def _save(self, reload=True):
        try:
            self.sup_id = self.db.save_supplier(self.sup_id, self._values())
        except (ValueError, sqlite3.Error) as e:
            QMessageBox.warning(self, "Suppliers", str(e))
            return False
        self.dirty = False
        if reload:
            self._load(self.sup_id)
        else:
            QTimer.singleShot(0, lambda: self._reload_keep())
        return True

    def _reload_keep(self):
        cur = self.list.currentItem()
        self._load(cur.data(Qt.UserRole) if cur else None)

    def _delete(self):
        if self.sup_id is None:
            return
        name = self.edits["name"].text()
        if QMessageBox.question(self, "Suppliers",
                                f"Delete supplier '{name}'?") != QMessageBox.Yes:
            return
        try:
            self.db.delete_supplier(self.sup_id)
        except (ValueError, sqlite3.Error) as e:
            QMessageBox.warning(self, "Suppliers", str(e))
            return
        self.dirty = False
        self.sup_id = None
        self._load()

    def reject(self):
        if self._maybe_save():
            super().reject()


def export_csv(widget, title, default_name, headers, rows):
    path, _ = QFileDialog.getSaveFileName(widget, title, default_name, "CSV (*.csv)")
    if not path:
        return
    import csv
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)


class TableReportDialog(QDialog):
    """A read-only table with optional filters on top, CSV export, and
    double-click to show a component in the main window."""

    def __init__(self, parent, title, headers, id_column=None):
        super().__init__(parent)
        self.main = parent
        self.headers, self.id_column = headers, id_column
        self.setWindowTitle(title)
        self.resize(1000, 560)
        self.lay = QVBoxLayout(self)
        self.top = QHBoxLayout()
        self.lay.addLayout(self.top)
        self.table = QTableWidget(0, len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().hide()
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.doubleClicked.connect(self._show_component)
        self.lay.addWidget(self.table, 1)
        bottom = QHBoxLayout()
        self.info = QLabel()
        bottom.addWidget(self.info, 1)
        exp = QPushButton("Export CSV…")
        exp.clicked.connect(self._export)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        bottom.addWidget(exp)
        bottom.addWidget(close)
        self.lay.addLayout(bottom)
        self.rows = []

    def set_rows(self, rows, colours=None):
        self.rows = rows
        t = self.table
        t.setSortingEnabled(False)
        t.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, v in enumerate(row):
                it = QTableWidgetItem()
                it.setData(Qt.DisplayRole, v if isinstance(v, (int, float)) else
                           ("" if v is None else str(v)))
                if colours and colours[r]:
                    it.setForeground(QColor(colours[r]))
                t.setItem(r, c, it)
        t.setSortingEnabled(True)
        t.resizeColumnsToContents()

    def _show_component(self, index):
        if self.id_column is None:
            return
        dev_id = self.table.item(index.row(), self.id_column).data(Qt.DisplayRole)
        if isinstance(dev_id, int):
            self.main.show_exact_id(dev_id)

    def _export(self):
        rows = [[self.table.item(r, c).text() for c in range(self.table.columnCount())]
                for r in range(self.table.rowCount())]
        export_csv(self, "Export CSV", self.windowTitle().lower().replace(" ", "_") + ".csv",
                   self.headers, rows)


class HistoryLogDialog(TableReportDialog):
    def __init__(self, parent, db):
        super().__init__(parent, "Stock history",
                         ["#", "Time", "ID", "Value", "Value2", "Δ", "Qty after",
                          "Reason", "Comment"], id_column=2)
        self.db = db
        today = QDate.currentDate()
        self.date_from = QDateEdit(today.addDays(-30), calendarPopup=True,
                                   displayFormat="yyyy-MM-dd")
        self.date_to = QDateEdit(today, calendarPopup=True, displayFormat="yyyy-MM-dd")
        self.reason = QComboBox()
        self.reason.addItem("all reasons", "")
        for r in db.history_reasons():
            self.reason.addItem(r, r)
        self.text = QLineEdit(placeholderText="value, comment or id",
                              clearButtonEnabled=True)
        for label, w in [("From:", self.date_from), ("To:", self.date_to),
                         ("Reason:", self.reason), ("Search:", self.text)]:
            self.top.addWidget(QLabel(label))
            self.top.addWidget(w, 1 if w is self.text else 0)
        self.date_from.dateChanged.connect(self.reload)
        self.date_to.dateChanged.connect(self.reload)
        self.reason.currentIndexChanged.connect(self.reload)
        self.text.textChanged.connect(self.reload)
        self.reload()

    def reload(self):
        rows = self.db.history_log(self.date_from.date().toString("yyyy-MM-dd"),
                                   self.date_to.date().toString("yyyy-MM-dd"),
                                   self.reason.currentData(), self.text.text())
        self.set_rows([[r["id"], r["timestamp"], r["dev_id"], r["value"], r["value2"],
                        r["delta"], r["quantity_after"], r["reason"], r["comment"]]
                       for r in rows],
                      ["green" if r["delta"] > 0 else "red" for r in rows])
        self.info.setText(f"{len(rows)} change(s), received "
                          f"{sum(r['delta'] for r in rows if r['delta'] > 0)}, used "
                          f"{-sum(r['delta'] for r in rows if r['delta'] < 0)} pcs. "
                          "Double-click a row to show the component.")


class LowStockDialog(TableReportDialog):
    def __init__(self, parent, db):
        super().__init__(parent, "Low stock report",
                         ["ID", "Value", "Value2", "Category", "Qty", "Warn at",
                          "Location", "Supplier", "Ordering code", "Price", "For (pcs)"],
                         id_column=0)
        self.top.addWidget(QLabel(
            "Components at or below their warning level, one row per source. "
            "Double-click a row to show the component."))
        rows = db.low_stock()
        self.set_rows([[r["id"], r["value"], r["value2"], r["category"], r["quantity"],
                        r["warn"], r["location"], r["supplier"], r["ordering_code"],
                        r["uprice"], r["ppu"]] for r in rows])
        self.info.setText(f"{len({r['id'] for r in rows})} component(s)")


class BomDialog(QDialog):
    """Deduct a whole BOM (bomer2.py CSV) from stock."""

    def __init__(self, parent, db):
        super().__init__(parent)
        self.db = db
        self.bom = None
        self.setWindowTitle("Deduct BOM from stock")
        self.resize(900, 560)
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        pick = QPushButton("Open BOM CSV…")
        pick.clicked.connect(self.pick)
        top.addWidget(pick)
        self.file = QLabel("<small>CSV written by bomer2.py in its default format "
                           "(not -c or -j)</small>")
        top.addWidget(self.file, 1)
        lay.addLayout(top)
        form = QHBoxLayout()
        form.addWidget(QLabel("Boards:"))
        self.boards = QSpinBox(minimum=1, maximum=10_000)
        self.boards.valueChanged.connect(self.update_table)
        form.addWidget(self.boards)
        form.addWidget(QLabel("Comment:"))
        self.comment = QLineEdit(placeholderText="project / board")
        form.addWidget(self.comment, 1)
        self.clamp = QCheckBox("When short, take only what is in stock")
        self.clamp.toggled.connect(self.update_table)
        form.addWidget(self.clamp)
        lay.addLayout(form)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["ID", "Value", "Refs", "Per board", "Needed", "In stock", "After"])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.table, 1)
        self.info = QLabel(wordWrap=True)
        lay.addWidget(self.info)
        bb = QHBoxLayout()
        bb.addStretch()
        self.go = QPushButton("Deduct")
        self.go.setEnabled(False)
        self.go.clicked.connect(self.deduct)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        bb.addWidget(self.go)
        bb.addWidget(close)
        lay.addLayout(bb)
        self.changes = []

    def pick(self):
        path, _ = QFileDialog.getOpenFileName(self, "BOM CSV", "", "CSV (*.csv);;All files (*)")
        if path:
            self.load(path)

    def load(self, path):
        try:
            self.bom = bom_csv.read(path)
        except (OSError, ValueError, UnicodeDecodeError) as e:
            QMessageBox.warning(self, "BOM", f"Cannot read {path}:\n\n{e}")
            return
        self.file.setText(html.escape(path))
        if not self.comment.text():
            self.comment.setText(os.path.basename(self.bom.source or path))
        self.update_table()

    def update_table(self):
        if self.bom is None:
            return
        boards = self.boards.value()
        t = self.table
        t.setRowCount(0)
        self.changes, short, unknown = [], [], []
        for line in self.bom.lines:
            dev = self.db.device(line.dev_id)
            if dev is None:
                unknown.append(line.dev_id)
                continue
            stock = self.db.stock(line.dev_id)
            have = (stock["quantity"] or 0) if stock else 0
            need = line.qty * boards
            take = min(need, have) if self.clamp.isChecked() else need
            if need > have:
                short.append(line.dev_id)
            if take:
                self.changes.append((line.dev_id, -take))
            r = t.rowCount()
            t.insertRow(r)
            for c, v in enumerate([line.dev_id, dev["value"], ",".join(line.refs),
                                   line.qty, need, have, have - take]):
                it = QTableWidgetItem("" if v is None else str(v))
                if need > have:
                    it.setForeground(QColor("red"))
                t.setItem(r, c, it)
        t.resizeColumnsToContents()
        msgs = [f"{len(self.bom.lines)} component(s), "
                f"{sum(-d for _, d in self.changes)} pcs to deduct."]
        if short:
            msgs.append(f"<span style='color:red'>Not enough in stock: "
                        f"{', '.join(f'#{i}' for i in short)}.</span>")
        if unknown:
            msgs.append(f"Ids not in the database (ignored): "
                        f"{', '.join(f'#{i}' for i in unknown)}.")
        if self.bom.skipped:
            msgs.append(f"Without an id (ignored): {', '.join(self.bom.skipped)}.")
        self.info.setText(" ".join(msgs))
        self.go.setEnabled(bool(self.changes) and (not short or self.clamp.isChecked()))

    def deduct(self):
        if QMessageBox.question(
                self, "Deduct BOM",
                f"Take {sum(-d for _, d in self.changes)} pcs of "
                f"{len(self.changes)} component(s) out of stock?") != QMessageBox.Yes:
            return
        try:
            self.db.change_stock_many(self.changes, "BOM", self.comment.text().strip())
        except (ValueError, sqlite3.Error) as e:
            QMessageBox.critical(self, "Deduct BOM", f"Nothing was changed:\n\n{e}")
            return
        QMessageBox.information(self, "Deduct BOM",
                                f"Done. {len(self.changes)} stock change(s) logged with "
                                "reason 'BOM'; each can be undone per component.")
        self.accept()


def printer_config(settings):
    return {k: settings.value(f"printer/{k}", v, type=type(v))
            for k, v in PRINTER_DEFAULTS.items()}


def image_for_printer(img, cfg):
    """Pad a narrower label to the print head width, centred (see
    PRINTER_DEFAULTS). The label is sent unrotated, so the width is padded."""
    head = cfg["head_width"]
    if not cfg["center"] or img.width() >= head:
        return img
    out = QImage(head, img.height(), QImage.Format_RGB32)
    out.fill(Qt.white)
    p = QPainter(out)
    p.drawImage((head - img.width()) // 2, 0, img)
    p.end()
    return out


def save_printer_config(settings, cfg):
    for k in PRINTER_DEFAULTS:
        settings.setValue(f"printer/{k}", cfg[k])


def print_command(cfg, image):
    """Return (argv, cwd). Arguments are split like a shell would, then
    {image} is substituted per argument, so paths with spaces are safe."""
    exe = os.path.expanduser(cfg["executable"].strip())
    args = [os.path.expanduser(a).replace("{image}", image)
            for a in shlex.split(cfg["arguments"])]
    cwd = os.path.expanduser(cfg["workdir"].strip()) or None
    return [exe] + args, cwd


def check_printer_config(cfg):
    """List of problems with the configuration (empty if it looks fine)."""
    problems = []
    exe = os.path.expanduser(cfg["executable"].strip())
    if not exe:
        problems.append("No executable set.")
    elif os.sep in exe:
        if not os.path.isfile(exe):
            problems.append(f"Executable not found: {exe}")
        elif not os.access(exe, os.X_OK):
            problems.append(f"Not executable: {exe}")
    elif not shutil.which(exe):
        problems.append(f"'{exe}' is not on the PATH.")
    try:
        args = shlex.split(cfg["arguments"])
        if not any("{image}" in a for a in args):
            problems.append("The arguments do not contain {image}.")
    except ValueError as e:
        problems.append(f"Arguments: {e}")
    wd = os.path.expanduser(cfg["workdir"].strip())
    if wd and not os.path.isdir(wd):
        problems.append(f"Working directory not found: {wd}")
    return problems


def run_print(cfg, image):
    """Run the printer command for one image. Returns (ok, output)."""
    argv, cwd = print_command(cfg, image)
    QGuiApplication.setOverrideCursor(Qt.WaitCursor)
    try:
        r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                           timeout=PRINT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    finally:
        QGuiApplication.restoreOverrideCursor()
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0:
        return False, f"exit code {r.returncode}\n{out}"
    return True, out


class PrinterSettingsDialog(QDialog):
    def __init__(self, parent, settings):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Printer settings")
        self.setMinimumWidth(620)
        cfg = printer_config(settings)
        form = QFormLayout(self)

        def path_row(label, text, pick):
            edit = QLineEdit(text)
            edit.textChanged.connect(self.update_preview)
            b = QPushButton("Browse…")
            b.clicked.connect(lambda: pick(edit))
            h = QHBoxLayout()
            h.addWidget(edit, 1)
            h.addWidget(b)
            form.addRow(label, h)
            return edit

        self.exe = path_row("Executable:", cfg["executable"], self._pick_exe)
        self.args = QLineEdit(cfg["arguments"])
        self.args.textChanged.connect(self.update_preview)
        form.addRow("Arguments:", self.args)
        self.workdir = path_row("Working directory:", cfg["workdir"], self._pick_dir)
        h = QHBoxLayout()
        self.center = QCheckBox("Centre the label on a print head of", checked=cfg["center"])
        self.head = QSpinBox(minimum=8, maximum=2000, value=cfg["head_width"], suffix=" px")
        self.center.toggled.connect(self.head.setEnabled)
        self.head.setEnabled(cfg["center"])
        h.addWidget(self.center)
        h.addWidget(self.head)
        h.addStretch()
        form.addRow("Alignment:", h)
        form.addRow(QLabel(
            "<small><code>{image}</code> is replaced by the rendered label PNG. "
            "<code>~</code> is your home directory. The working directory may "
            "be empty.</small>", wordWrap=True))
        self.preview = QLabel(wordWrap=True,
                              textInteractionFlags=Qt.TextSelectableByMouse)
        form.addRow("Command:", self.preview)
        self.problems = QLabel(wordWrap=True)
        form.addRow(self.problems)

        h = QHBoxLayout()
        defaults = QPushButton("Defaults")
        defaults.clicked.connect(self._defaults)
        test = QPushButton("Print test label")
        test.clicked.connect(self._test)
        h.addWidget(defaults)
        h.addWidget(test)
        h.addStretch()
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        h.addWidget(bb)
        form.addRow(h)
        self.update_preview()

    def config(self):
        return {"executable": self.exe.text(), "arguments": self.args.text(),
                "workdir": self.workdir.text(), "center": self.center.isChecked(),
                "head_width": self.head.value()}

    def update_preview(self):
        cfg = self.config()
        try:
            argv, cwd = print_command(cfg, "<label.png>")
            text = f"<code>{html.escape(shlex.join(argv))}</code>"
            if cwd:
                text += f"<br><small>in {html.escape(cwd)}</small>"
        except ValueError as e:
            text = str(e)
        self.preview.setText(text)
        problems = check_printer_config(cfg)
        self.problems.setText(
            "<span style='color:red'>" + "<br>".join(problems) + "</span>"
            if problems else "<span style='color:green'>Looks OK.</span>")

    def _pick_exe(self, edit):
        path, _ = QFileDialog.getOpenFileName(
            self, "Printer executable", os.path.expanduser(edit.text()) or
            os.path.expanduser("~"))
        if path:
            edit.setText(path)

    def _pick_dir(self, edit):
        path = QFileDialog.getExistingDirectory(
            self, "Working directory", os.path.expanduser(edit.text()) or
            os.path.expanduser("~"))
        if path:
            edit.setText(path)

    def _defaults(self):
        self.exe.setText(PRINTER_DEFAULTS["executable"])
        self.args.setText(PRINTER_DEFAULTS["arguments"])
        self.workdir.setText(PRINTER_DEFAULTS["workdir"])
        self.center.setChecked(PRINTER_DEFAULTS["center"])
        self.head.setValue(PRINTER_DEFAULTS["head_width"])

    def _test(self):
        LABEL_W, LABEL_H = label_size_px(self.settings.value("label/size", "", type=str))
        img = QImage(LABEL_W, LABEL_H, QImage.Format_RGB32)
        img.fill(Qt.white)
        p = QPainter(img)
        pen = p.pen()
        pen.setWidth(3)
        p.setPen(pen)
        p.drawRect(1, 1, LABEL_W - 3, LABEL_H - 3)  # frame: all edges visible?
        p.setFont(QFont("Sans", 11, QFont.Bold))
        p.drawText(0, 6, LABEL_W, 20, Qt.AlignHCenter, "TOP")
        p.drawText(0, LABEL_H - 26, LABEL_W, 20, Qt.AlignHCenter, "BOTTOM")
        pen.setWidth(1)
        p.setPen(pen)
        p.setFont(QFont("Sans", 7))
        for mm in range(LABEL_H // 8 + 1):  # ruler, 8 px = 1 mm, from the top
            y = mm * 8
            p.drawLine(0, y, 14 if mm % 5 == 0 else 7, y)
            if mm % 5 == 0 and 0 < mm:
                p.drawText(17, y - 6, 30, 12, Qt.AlignVCenter, f"{mm}")
        p.setFont(QFont("Sans", 18, QFont.Bold))
        p.drawText(img.rect(), Qt.AlignCenter,
                   f"Test label\n{LABEL_W // 8} × {LABEL_H // 8} mm")
        p.end()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test_label.png")
            image_for_printer(img, self.config()).save(path)
            ok, out = run_print(self.config(), path)
        (QMessageBox.information if ok else QMessageBox.warning)(
            self, "Test print", ("Printed." if ok else "Printing failed.")
            + (f"\n\n{out}" if out else ""))

    def accept(self):
        problems = check_printer_config(self.config())
        if problems and QMessageBox.question(
                self, "Printer settings",
                "\n".join(problems) + "\n\nSave anyway?") != QMessageBox.Yes:
            return
        save_printer_config(self.settings, self.config())
        super().accept()


class LabelOptions(QWidget):
    """Label template and QR code options. Every change is saved at once in
    the settings file ([label] section) and announced with `changed`. Used by
    both the print dialog and Settings → Label…."""

    changed = Signal()

    def __init__(self, settings):
        super().__init__()
        st = self.settings = settings
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        box = QGroupBox("Template")
        f = QFormLayout(box)
        # Template choices are remembered in the settings file ([label] section).
        self.opts = {}
        for key, text in [("value2", "Value2 / Value3"), ("footprint", "Footprint"),
                          ("category", "Category"), ("description", "Description"),
                          ("location", "Location")]:
            cb = QCheckBox(text, checked=st.value(f"label/{key}",
                                                  key != "description", type=bool))
            cb.toggled.connect(lambda on, k=key: st.setValue(f"label/{k}", on))
            cb.toggled.connect(self.changed)
            self.opts[key] = cb
            f.addRow(cb)
            if key == "footprint":
                # "Capacitor_SMD:C_0603_1608Metric" -> "C_0603_1608Metric"
                sub = QCheckBox("without library name", checked=st.value(
                    "label/short_footprint", True, type=bool))
                sub.setToolTip("Print only the part after ':' of the footprint")
                sub.toggled.connect(lambda on: st.setValue("label/short_footprint", on))
                sub.toggled.connect(self.changed)
                sub.setEnabled(cb.isChecked())
                cb.toggled.connect(sub.setEnabled)
                f.addRow("    ", sub)
                self.opts["short_footprint"] = sub
        self.font_size = QSpinBox(minimum=8, maximum=30,
                                  value=st.value("label/font_size", 16, type=int))
        self.font_size.valueChanged.connect(lambda v: st.setValue("label/font_size", v))
        self.font_size.valueChanged.connect(self.changed)
        f.addRow("Font size:", self.font_size)
        self.size = QComboBox()
        self.size.addItems([n for n, _, _ in LABEL_SIZES])
        self.size.setCurrentText(st.value("label/size", self.size.itemText(0), type=str))
        self.size.currentTextChanged.connect(lambda t: st.setValue("label/size", t))
        self.size.currentTextChanged.connect(self.changed)
        f.addRow("Label:", self.size)
        lay.addWidget(box)

        box = QGroupBox("QR code")
        f = QFormLayout(box)
        self.qr_ec = QComboBox()
        for level, text in QR_EC_LEVELS:
            self.qr_ec.addItem(text, level)
        self.qr_ec.setToolTip("Higher levels survive more damage, but need more "
                              "and therefore smaller modules")
        self.qr_ec.setCurrentIndex(max(0, self.qr_ec.findData(
            st.value("label/qr_ec", "H", type=str))))
        self.qr_ec.currentIndexChanged.connect(
            lambda: st.setValue("label/qr_ec", self.qr_ec.currentData()))
        self.qr_ec.currentIndexChanged.connect(self.changed)
        f.addRow("Error correction:", self.qr_ec)
        self.qr_border = QSpinBox(minimum=0, maximum=4, suffix=" modules",
                                  value=st.value("label/qr_border", 0, type=int))
        self.qr_border.setToolTip("White border around the code. The QR standard "
                                  "asks for 4; most scanners manage with less.")
        self.qr_border.valueChanged.connect(lambda v: st.setValue("label/qr_border", v))
        self.qr_border.valueChanged.connect(self.changed)
        f.addRow("Quiet zone:", self.qr_border)
        self.qr_info = QLabel()
        f.addRow(self.qr_info)
        lay.addWidget(box)

    def values(self):
        """Options for render_label()."""
        opts = {k: cb.isChecked() for k, cb in self.opts.items()}
        opts["font_size"] = self.font_size.value()
        opts["qr_ec"] = self.qr_ec.currentData()
        opts["qr_border"] = self.qr_border.value()
        opts["size_px"] = label_size_px(self.size.currentText())
        return opts

    def set_qr_info(self, text):
        self.qr_info.setText(f"<small>{text}</small>")


class LabelSettingsDialog(QDialog):
    """Settings → Label…: the label options with a preview of one component."""

    def __init__(self, parent, settings, db, dev_id):
        super().__init__(parent)
        self.db, self.dev_id = db, dev_id
        self.setWindowTitle("Label settings")
        lay = QHBoxLayout(self)
        left = QVBoxLayout()
        self.preview = QLabel(alignment=Qt.AlignCenter)
        self.preview.setStyleSheet("background:#888; padding:12px;")
        left.addWidget(self.preview, 1)
        left.addWidget(QLabel(f"<small>Preview: component #{dev_id}. Changes "
                              "are saved immediately.</small>"))
        lay.addLayout(left, 1)
        right = QVBoxLayout()
        self.options = LabelOptions(settings)
        self.options.changed.connect(self.update_preview)
        right.addWidget(self.options)
        right.addStretch()
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        right.addWidget(bb)
        lay.addLayout(right)
        self.update_preview()

    def update_preview(self):
        img = component_label(self.db, self.dev_id, self.options.values())
        self.preview.setPixmap(QPixmap.fromImage(img))
        self.options.set_qr_info(img.text("qr_info"))


def component_label(db, dev_id, opts):
    cad = db.cad(dev_id)
    stock = db.stock(dev_id)
    return render_label(db.device(dev_id), cad["footprint"] if cad else "",
                        stock["location"] if stock else None, opts)


class LabelDialog(QDialog):
    def __init__(self, parent, db, dev_ids):
        super().__init__(parent)
        self.settings = parent.settings
        self.db = db
        self.dev_ids = dev_ids
        self.setWindowTitle(f"Print labels ({len(dev_ids)})")
        lay = QHBoxLayout(self)

        left = QVBoxLayout()
        self.preview = QLabel(alignment=Qt.AlignCenter)
        self.preview.setStyleSheet("background:#888; padding:12px;")
        left.addWidget(self.preview)
        nav = QHBoxLayout()
        self.idx = 0
        prev_b, next_b = QPushButton("◀"), QPushButton("▶")
        self.pos = QLabel()
        prev_b.clicked.connect(lambda: self.step(-1))
        next_b.clicked.connect(lambda: self.step(1))
        nav.addWidget(prev_b)
        nav.addWidget(self.pos, 1, Qt.AlignCenter)
        nav.addWidget(next_b)
        left.addLayout(nav)
        lay.addLayout(left, 1)

        right = QVBoxLayout()
        self.options = LabelOptions(self.settings)
        self.options.changed.connect(self.update_preview)
        right.addWidget(self.options)

        box = QGroupBox("Printer")
        f = QFormLayout(box)
        self.cmd = QLabel(wordWrap=True)
        self.cmd.setMinimumWidth(300)
        f.addRow(self.cmd)
        change = QPushButton("Printer settings…")
        change.clicked.connect(self.printer_settings)
        f.addRow(change)
        self._show_command()
        self.copies = QSpinBox(minimum=1, maximum=50)
        f.addRow("Copies:", self.copies)
        right.addWidget(box)

        self.log = QPlainTextEdit(readOnly=True, maximumHeight=80)
        right.addWidget(self.log)
        right.addStretch()
        h = QHBoxLayout()
        save = QPushButton("Save…")
        save.setToolTip("PNG: the current label. PDF: all labels, one per page.")
        save.clicked.connect(self.save_labels)
        prn = QPushButton("Print")
        prn.setDefault(True)
        prn.clicked.connect(self.print_labels)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        h.addWidget(save)
        h.addStretch()
        h.addWidget(prn)
        h.addWidget(close)
        right.addLayout(h)
        lay.addLayout(right)
        self.update_preview()

    def step(self, d):
        self.idx = (self.idx + d) % len(self.dev_ids)
        self.update_preview()

    def _show_command(self):
        argv, _ = print_command(printer_config(self.settings), "{image}")
        self.cmd.setText(f"<small><code>{html.escape(shlex.join(argv))}</code></small>")

    def printer_settings(self):
        PrinterSettingsDialog(self, self.settings).exec()
        self._show_command()

    def current_image(self):
        return self.image_for(self.dev_ids[self.idx])

    def image_for(self, dev_id):
        return component_label(self.db, dev_id, self.options.values())

    def update_preview(self):
        self.image = self.current_image()
        self.preview.setPixmap(QPixmap.fromImage(self.image))
        self.options.set_qr_info(self.image.text("qr_info"))
        self.pos.setText(f"{self.idx + 1} / {len(self.dev_ids)}")

    def save_labels(self):
        png = "PNG image, current label (*.png)"
        pdf = "PDF, all labels, one per page (*.pdf)"
        path, flt = QFileDialog.getSaveFileName(
            self, "Save labels", f"label_{self.dev_ids[self.idx]}.png", f"{png};;{pdf}")
        if not path:
            return
        if flt == pdf or path.lower().endswith(".pdf"):
            if not path.lower().endswith(".pdf"):
                path = os.path.splitext(path)[0] + ".pdf"
            write_labels_pdf(path, [self.image_for(i) for i in self.dev_ids])
            self.log.appendPlainText(f"Saved {len(self.dev_ids)} label(s) to {path}")
        else:
            if not path.lower().endswith(".png"):
                path += ".png"
            self.image.save(path)
            self.log.appendPlainText(f"Saved {path}")

    def print_labels(self):
        cfg = printer_config(self.settings)
        problems = check_printer_config(cfg)
        if problems:
            QMessageBox.warning(self, "Printer", "\n".join(problems)
                                + "\n\nCheck Printer settings.")
            return
        total = len(self.dev_ids) * self.copies.value()
        done = 0
        with tempfile.TemporaryDirectory(prefix="labels-") as tmp:
            for dev_id in self.dev_ids:
                path = os.path.join(tmp, f"label_{dev_id}.png")
                image_for_printer(self.image_for(dev_id), cfg).save(path)
                for _ in range(self.copies.value()):
                    self.log.appendPlainText(f"#{dev_id}: printing ({done + 1}/{total})…")
                    QApplication.processEvents()
                    ok, out = run_print(cfg, path)
                    if out:
                        self.log.appendPlainText(out)
                    if not ok:
                        self.log.appendPlainText(f"#{dev_id}: FAILED, stopped.")
                        QMessageBox.warning(self, "Printing failed",
                                            f"Label #{dev_id} failed:\n\n{out}")
                        return
                    done += 1
        self.log.appendPlainText(f"Done: {done} label(s) printed.")


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------

class ScanLineEdit(QLineEdit):
    """Scan field. Keeps the control characters GS / RS / EOT of ISO 15434
    codes (normally dropped by QLineEdit) as the visible symbols ␝ ␞ ␄."""

    def keyPressEvent(self, event):
        text = event.text()
        if text and text in distributor_codes.VISIBLE:
            self.insert(distributor_codes.VISIBLE[text])
            return
        super().keyPressEvent(event)


class DetailPanel(QTabWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main

        # Overview
        w = QWidget()
        v = QVBoxLayout(w)
        self.title = QLabel(textFormat=Qt.RichText, wordWrap=True)
        self.title.setStyleSheet("font-size:16pt;")
        v.addWidget(self.title)
        self.info = QFormLayout()
        self.fields = {}
        for key in ["Category", "Type", "Technology", "Description",
                    "Symbol", "Footprint", "3D model", "Datasheet", "Comment"]:
            lbl = QLabel(wordWrap=True, textInteractionFlags=Qt.TextBrowserInteraction,
                         openExternalLinks=True)
            self.fields[key] = lbl
            self.info.addRow(f"<b>{key}:</b>", lbl)
        v.addLayout(self.info)

        stock_box = QGroupBox("Stock")
        sh = QHBoxLayout(stock_box)
        self.qty = QLabel()
        self.qty.setStyleSheet("font-size:22pt; font-weight:bold;")
        self.qty.setMinimumWidth(110)
        self.qty.setAlignment(Qt.AlignCenter)
        sh.addWidget(self.qty)
        self.stock_info = QLabel()
        sh.addWidget(self.stock_info, 1)
        sv = QVBoxLayout()
        for text, act in [("Receive (+)", main.act_receive),
                          ("Consume (−)", main.act_consume),
                          ("Settings…", main.act_stock_settings)]:
            b = QPushButton(text)
            b.clicked.connect(act.trigger)
            sv.addWidget(b)
        sh.addLayout(sv)
        v.addWidget(stock_box)
        v.addStretch()
        self.addTab(w, "Overview")

        # Sources
        self.sources = QTableWidget(0, 5)
        self.sources.setHorizontalHeaderLabels(
            ["Supplier", "Ordering code", "Price", "For (pcs)", "Comment"])
        self.sources.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.sources.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.addTab(self.sources, "Sources")

        # History
        self.history = QTableWidget(0, 5)
        self.history.setHorizontalHeaderLabels(
            ["Time", "Δ", "Qty after", "Reason", "Comment"])
        self.history.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.history.setEditTriggers(QAbstractItemView.NoEditTriggers)
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(self.history, 1)
        hb = QHBoxLayout()
        self.undo_info = QLabel()
        hb.addWidget(self.undo_info, 1)
        b = QPushButton("Undo last change")
        b.clicked.connect(main.act_undo.trigger)
        hb.addWidget(b)
        v.addLayout(hb)
        self.addTab(w, "History")

    def clear_device(self):
        self.title.setText("<span style='color:gray'>No component selected</span>")
        for lbl in self.fields.values():
            lbl.clear()
        self.qty.setText("")
        self.stock_info.clear()
        self.sources.setRowCount(0)
        self.history.setRowCount(0)
        self.undo_info.clear()

    def show_device(self, db, dev_id):
        dev = db.device(dev_id)
        if dev is None:
            return
        cad = db.cad(dev_id)
        stock = db.stock(dev_id)

        def s(v):
            return "" if v is None else str(v)

        self.title.setText(f"<b>{s(dev['value'])}</b> "
                           f"<span style='color:gray'>{s(dev['value2'])} "
                           f"{s(dev['value3'])} · #{dev_id}</span>")
        doc = s(dev["documentation"])
        vals = {
            "Category": dev["category_name"], "Type": dev["type"],
            "Technology": dev["technology"], "Description": dev["description"],
            "Symbol": cad["symbol"] if cad else None,
            "Footprint": cad["footprint"] if cad else None,
            "3D model": cad["mech_model"] if cad else None,
            "Datasheet": f"<a href='{doc}'>{doc}</a>" if doc else "",
            "Comment": dev["comment"],
        }
        for k, v in vals.items():
            self.fields[k].setText(s(v))

        if stock is None:
            self.qty.setText("—")
            self.stock_info.setText("no stock record")
        else:
            q, warn = stock["quantity"] or 0, stock["warn"] or 0
            self.qty.setText(str(q))
            colour = "red" if q <= warn else "green"
            self.qty.setStyleSheet(f"font-size:22pt; font-weight:bold; color:{colour};")
            self.stock_info.setText(
                f"warn at: {warn}<br>location: <b>{s(stock['location']) or '—'}</b>")

        rows = db.sources(dev_id)
        self.sources.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, k in enumerate(["supplier", "ordering_code", "uprice", "ppu", "comment"]):
                self.sources.setItem(r, c, QTableWidgetItem(s(row[k])))

        last = db.last_undoable(dev_id) if db.has_history else None
        self.undo_info.setText(
            f"<small>Undo would reverse: {last['timestamp']}, {last['delta']:+d} "
            f"({last['reason']})</small>" if last else "<small>Nothing to undo.</small>")
        rows = db.history(dev_id)
        self.history.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, k in enumerate(["timestamp", "delta", "quantity_after", "reason", "comment"]):
                self.history.setItem(r, c, QTableWidgetItem(s(row[k])))


class MainWindow(QMainWindow):
    COLUMNS = ["ID", "Value", "Value2", "Value3", "Category", "Footprint",
               "Qty", "Location"]

    def __init__(self, db):
        super().__init__()
        self.db = None
        self.settings = QSettings("kicad_utils", "inventory_gui")
        self.backup_label = QLabel()
        self.statusBar().addPermanentWidget(self.backup_label)
        self.resize(1400, 820)
        self._make_actions()

        central = QWidget()
        root = QVBoxLayout(central)

        # Scan + search bar
        bar = QHBoxLayout()
        self.scan = ScanLineEdit(placeholderText="Scan label or distributor barcode…")
        self.scan.setStyleSheet("font-size:14pt; padding:4px; "
                                "border:2px solid #3a7; border-radius:4px;")
        self.scan.returnPressed.connect(self.on_scan)
        bar.addWidget(QLabel("<b>Scan:</b>"))
        bar.addWidget(self.scan, 2)
        self.search = QLineEdit(placeholderText="Search value, footprint, "
                                "ordering code, location… (id:23 = exact id)",
                                clearButtonEnabled=True)
        self.search.textChanged.connect(self.refresh)
        bar.addWidget(self.search, 3)
        self.stock_filter = QComboBox()
        self.stock_filter.addItems(["all", "in stock", "out of stock", "low stock"])
        self.stock_filter.currentTextChanged.connect(self.refresh)
        bar.addWidget(self.stock_filter)
        root.addLayout(bar)

        # Category tree | results | details
        split = QSplitter()
        self.tree = QTreeWidget()
        self.tree.setHeaderLabel("Categories")
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.on_tree_menu)
        self.tree.itemSelectionChanged.connect(self.refresh)
        split.addWidget(self.tree)

        self.model = QStandardItemModel(0, len(self.COLUMNS))
        self.model.setHorizontalHeaderLabels(self.COLUMNS)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.selectionModel().currentRowChanged.connect(self.on_row)
        self.table.doubleClicked.connect(self.act_edit.trigger)
        split.addWidget(self.table)

        self.detail = DetailPanel(self)
        split.addWidget(self.detail)
        split.setSizes([230, 700, 470])
        root.addWidget(split, 1)
        self.setCentralWidget(central)

        self.set_database(db)
        self.scan.setFocus()

    def _make_menus(self):
        A = self.actions_by_name
        menu = self.menuBar().addMenu("&File")
        a = menu.addAction("&Open database…", self.on_open)
        a.setShortcut(QKeySequence.Open)
        self.recent_menu = menu.addMenu("Open &recent")
        self.recent_menu.aboutToShow.connect(self._fill_recent_menu)
        a = menu.addAction("Re&load", lambda: self.open_database(self.db.path))
        a.setShortcut(QKeySequence("Ctrl+R"))
        menu.addSeparator()
        a = menu.addAction("&Quit", self.close)
        a.setShortcut(QKeySequence.Quit)

        menu = self.menuBar().addMenu("&Component")
        for name in ("new", "edit", "duplicate", "delete", None, "print", None,
                     "suppliers"):
            menu.addSeparator() if name is None else menu.addAction(A[name])

        menu = self.menuBar().addMenu("S&tock")
        for name in ("receive", "consume", "stock_settings", "undo", None, "bom",
                     "low_stock", "history"):
            menu.addSeparator() if name is None else menu.addAction(A[name])

        menu = self.menuBar().addMenu("&Settings")
        menu.addAction("&Label…", self.on_label_settings)
        menu.addAction("&Printer…",
                       lambda: PrinterSettingsDialog(self, self.settings).exec())

        menu = self.menuBar().addMenu("&Help")
        menu.addAction("&About…", self.on_about)
        menu.addAction("About &Qt…", QApplication.aboutQt)

    def on_about(self):
        def version(package):
            try:
                from importlib.metadata import version as v
                return v(package)
            except Exception:
                return "not installed"

        here = os.path.dirname(os.path.abspath(__file__))
        try:
            rev = subprocess.run(
                ["git", "-C", here, "describe", "--always", "--dirty"],
                capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            rev = ""
        e = html.escape
        QMessageBox.about(self, f"About {APP_NAME}", f"""
            <h3>{APP_NAME}</h3>
            <p>Manage the electronic component database used by the KiCad
            database library and <code>bomer2.py</code>: search, add
            components, track stock, print QR code labels for the storage.</p>
            <p>Part of <a href="{PROJECT_URL}">kicad_utils</a>
            {f"(git {e(rev)})" if rev else ""}.<br>MIT License.</p>
            <table>
            <tr><td>Database:</td><td>{e(self.db.path)}</td></tr>
            <tr><td>Settings:</td><td>{e(self.settings.fileName())}</td></tr>
            <tr><td>Python:</td><td>{sys.version.split()[0]}</td></tr>
            <tr><td>PySide6 / Qt:</td><td>{version("PySide6")} / {qVersion()}</td></tr>
            <tr><td>qrcode:</td><td>{version("qrcode")}</td></tr>
            </table>""")

    def _fill_recent_menu(self):
        self.recent_menu.clear()
        recent = recent_files(self.settings)
        for path in recent:
            self.recent_menu.addAction(path, lambda p=path: self.open_database(p))
        if recent:
            self.recent_menu.addSeparator()
            self.recent_menu.addAction("Clear list", lambda: self.settings.setValue("recent", []))
        else:
            self.recent_menu.addAction("(empty)").setEnabled(False)

    def on_open(self):
        start = os.path.dirname(self.db.path) if self.db else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Open component database", start,
            "SQLite databases (*.sqlite *.sqlite3 *.db);;All files (*)")
        if path:
            self.open_database(path)

    def open_database(self, path):
        """Open `path`, keeping the current database if it fails."""
        try:
            db = Database(path)
        except (sqlite3.Error, ValueError) as e:
            QMessageBox.critical(self, "Cannot open database", f"{path}\n\n{e}")
            return False
        self.set_database(db)
        return True

    def set_database(self, db):
        if self.db is not None and self.db is not db:
            self.db.con.close()
        self.db = db
        db.on_backup = self._show_backup
        self._show_backup(None)
        remember_file(self.settings, db.path)
        if db.needs_migration():
            self._ask_migration(db)
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.tree.blockSignals(True)
        self.tree.clear()
        self._load_categories()
        self.tree.blockSignals(False)
        self.detail.clear_device()
        self.refresh()

        self.statusBar().showMessage(
            f"Opened {db.path}."
            + (" Not upgraded: stock changes are disabled." if db.needs_migration() else ""))

    def _show_backup(self, path):
        self.backup_label.setText(f"Backup: {os.path.basename(path)}" if path
                                  else "No changes yet")
        self.backup_label.setToolTip(path or "A backup is made before the first change.")

    def _ask_migration(self, db):
        todo = []
        if not db.has_location:
            todo.append("• add a <code>location</code> column to <code>stock</code>")
        if not db.has_history:
            todo.append("• create the <code>stock_history</code> table")
        ans = QMessageBox.question(
            self, "Upgrade database",
            f"<p>{db.path} needs a one-time upgrade to save stock changes:</p>"
            f"<p>{'<br>'.join(todo)}</p>"
            "<p>Existing tables and data are not changed, so KiCad and "
            "bomer2.py keep working. A backup copy is made first.</p>"
            "<p>Upgrade now?</p>")
        if ans == QMessageBox.Yes:
            try:
                db.migrate()
            except sqlite3.Error as e:
                QMessageBox.critical(self, "Upgrade failed", str(e))

    # --- categories --------------------------------------------------------

    def reload_categories(self, select=None):
        """Rebuild the category tree, selecting `select` (default: keep)."""
        if select is None:
            sel = self.tree.selectedItems()
            select = sel[0].data(0, Qt.UserRole) if sel else None
        self.tree.blockSignals(True)
        self.tree.clear()
        self._load_categories()
        for it in self._all_tree_items():
            if select is not None and it.data(0, Qt.UserRole) == select:
                self.tree.setCurrentItem(it)
                self.tree.scrollToItem(it)
        self.tree.blockSignals(False)
        self.refresh()

    def _all_tree_items(self):
        todo = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while todo:
            it = todo.pop()
            yield it
            todo += [it.child(i) for i in range(it.childCount())]

    def on_tree_menu(self, pos):
        item = self.tree.itemAt(pos)
        cid = item.data(0, Qt.UserRole) if item else None
        if cid is None:
            return
        menu = QMenu(self)
        menu.addAction("New subcategory…", lambda: self.on_category_new(cid))
        menu.addAction("Rename…", lambda: self.on_category_rename(cid))
        menu.addAction("Edit comment…", lambda: self.on_category_comment(cid))
        menu.addSeparator()
        menu.addAction("Delete", lambda: self.on_category_delete(cid))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def on_category_new(self, parent):
        new_id = ask_new_category(self, self.db, parent)
        if new_id is not None:
            self.reload_categories(new_id)
            self.statusBar().showMessage(f"Added category #{new_id}", 5000)

    def on_category_rename(self, cid):
        cat = self.db.category(cid)
        name, ok = QInputDialog.getText(self, "Rename category", "Name:",
                                        text=cat["name"] or "")
        if ok:
            try:
                self.db.rename_category(cid, name)
            except (ValueError, sqlite3.Error) as e:
                QMessageBox.warning(self, "Rename category", str(e))
                return
            self.reload_categories(cid)

    def on_category_comment(self, cid):
        cat = self.db.category(cid)
        text, ok = QInputDialog.getMultiLineText(
            self, "Category comment", f"Comment for '{cat['name']}':",
            cat["comment"] or "")
        if ok:
            try:
                self.db.set_category_comment(cid, text.strip())
            except sqlite3.Error as e:
                QMessageBox.warning(self, "Category comment", str(e))

    def on_category_delete(self, cid):
        cat = self.db.category(cid)
        if QMessageBox.question(self, "Delete category",
                                f"Delete category '{cat['name']}'?") != QMessageBox.Yes:
            return
        try:
            self.db.delete_category(cid)
        except (ValueError, sqlite3.Error) as e:
            QMessageBox.warning(self, "Delete category", str(e))
            return
        self.reload_categories(cat["parent"])

    def _make_actions(self):
        def act(name, text, slot, key=None, tip=None):
            a = QAction(text, self)
            if key:
                a.setShortcut(QKeySequence(key))
            if tip:
                a.setToolTip(tip)
            a.triggered.connect(slot)
            self.actions_by_name[name] = a
            return a

        self.actions_by_name = {}
        self.act_new = act("new", "New…", lambda: self.on_new(), "Ctrl+N")
        self.act_edit = act("edit", "Edit…", self.on_edit, "Ctrl+E")
        self.act_dup = act("duplicate", "Duplicate…", self.on_dup, "Ctrl+D")
        act("delete", "Delete…", self.on_delete, "Shift+Del")
        act("print", "Print label…", self.on_print, "Ctrl+P")
        act("suppliers", "Suppliers…", self.on_suppliers)
        self.act_receive = act("receive", "Receive +", lambda: self.on_stock(True), "F2")
        self.act_consume = act("consume", "Consume −", lambda: self.on_stock(False), "F3")
        self.act_stock_settings = act("stock_settings", "Stock settings…",
                                      self.on_stock_settings, "F4")
        self.act_undo = act("undo", "Undo last stock change", self.on_undo, "Ctrl+Z",
                            "Reverse the last stock change of the selected component")
        act("bom", "Deduct BOM…", self.on_bom)
        act("low_stock", "Low stock report…", self.on_low_stock)
        act("history", "Stock history…", self.on_history)
        act("focus_scan", "Focus scan", self.scan_focus, "F1")
        act("focus_search", "Focus search", lambda: self.search.setFocus(), "Ctrl+F")
        self._make_menus()

        tb = QToolBar("Main")
        tb.setIconSize(QSize(16, 16))
        tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.addToolBar(tb)
        A = self.actions_by_name
        for name in ("new", "edit", "duplicate", None, "receive", "consume",
                     "stock_settings", "undo", None, "print", None, "low_stock",
                     "history", "bom", None, "focus_scan", "focus_search"):
            tb.addSeparator() if name is None else tb.addAction(A[name])

    def _load_categories(self):
        _, self.children = fill_category_tree(self.tree, self.db.categories())
        all_item = QTreeWidgetItem(["(all)"])
        all_item.setData(0, Qt.UserRole, None)
        self.tree.insertTopLevelItem(0, all_item)

    def _descendants(self, cid):
        out, todo = set(), [cid]
        while todo:
            c = todo.pop()
            if c not in out:
                out.add(c)
                todo += self.children.get(c, [])
        return out

    # --- slots -------------------------------------------------------------

    def refresh(self):
        sel = self.tree.selectedItems()
        cid = sel[0].data(0, Qt.UserRole) if sel else None
        cats = self._descendants(cid) if cid is not None else None
        rows = self.db.search(self.search.text(), cats,
                              self.stock_filter.currentText())
        self.table.setSortingEnabled(False)
        self.model.setRowCount(0)
        for r in rows:
            items = []
            for k in ["id", "value", "value2", "value3", "category",
                      "footprint", "quantity", "location"]:
                it = QStandardItem()
                v = r[k]
                it.setData(v if isinstance(v, int) else ("" if v is None else str(v)),
                           Qt.DisplayRole)
                items.append(it)
            q, warn = r["quantity"], r["warn"]
            if q is not None and q <= (warn or 0):
                items[6].setForeground(QColor("red"))
            self.model.appendRow(items)
        self.table.setSortingEnabled(True)
        self.table.resizeColumnsToContents()
        self.setWindowTitle(f"Component inventory — {os.path.basename(self.db.path)}"
                            f" — {len(rows)} components")

    def current_id(self):
        idx = self.table.currentIndex()
        if not idx.isValid():
            return None
        return self.model.item(idx.row(), 0).data(Qt.DisplayRole)

    def selected_ids(self):
        rows = {i.row() for i in self.table.selectionModel().selectedRows()}
        return [self.model.item(r, 0).data(Qt.DisplayRole) for r in sorted(rows)]

    def on_row(self, cur, _prev):
        if cur.isValid():
            self.detail.show_device(self.db, self.model.item(cur.row(), 0).data(Qt.DisplayRole))

    def select_id(self, dev_id):
        for r in range(self.model.rowCount()):
            if self.model.item(r, 0).data(Qt.DisplayRole) == dev_id:
                self.table.selectRow(r)
                self.table.scrollTo(self.model.index(r, 0))
                return True
        return False

    def scan_focus(self):
        self.scan.setFocus()
        self.scan.selectAll()

    def on_scan(self):
        code = self.scan.text().strip()
        self.scan.clear()
        if not code:
            return
        # Our own label: "KC:<id>". Always treated as an exact id.
        if code.upper().startswith(LABEL_PREFIX):
            rest = code[len(LABEL_PREFIX):].strip()
            if rest.isdigit():
                dev_id = int(rest)
                if not self.show_exact_id(dev_id):
                    self.statusBar().showMessage(
                        f"Scanned label, but there is no component #{dev_id}", 8000)
                return
        # A bare number (older labels): exact id if such a component exists.
        if code.isdigit() and self.show_exact_id(int(code)):
            return
        dc = distributor_codes.parse(code)
        if dc is not None:
            self.on_distributor_code(dc)
            return
        self.search.setText(code)
        self.statusBar().showMessage(f"Unknown code, searching for {code!r}", 5000)

    def show_exact_id(self, dev_id):
        """Filter the list to exactly `dev_id` and select it. Returns False
        (and changes nothing) if there is no such component."""
        if self.db.device(dev_id) is None:
            return False
        for w in (self.tree, self.stock_filter):
            w.blockSignals(True)
        self.tree.clearSelection()
        self.stock_filter.setCurrentText("all")
        for w in (self.tree, self.stock_filter):
            w.blockSignals(False)
        self.search.setText(f"id:{dev_id}")  # triggers refresh()
        self.refresh()  # in case the text was already the same
        self.select_id(dev_id)
        self.detail.show_device(self.db, dev_id)
        self.statusBar().showMessage(f"Scanned component #{dev_id}", 5000)
        return True

    def need_selection(self):
        dev_id = self.current_id()
        if dev_id is None:
            QMessageBox.information(self, "No selection", "Select a component first.")
        return dev_id

    def on_distributor_code(self, dc):
        """A scanned distributor bag: find the component and receive the
        quantity, or offer to create a new component."""
        if dc.error:
            QMessageBox.warning(self, f"{dc.distributor} code", dc.error)
            return
        sup_id = self.db.supplier_by_names(
            distributor_codes.SUPPLIER_NAMES.get(dc.distributor, ()))
        matches = self.db.find_by_distributor(dc, sup_id)
        comment = f"{dc.distributor} order {dc.order}" if dc.order else dc.distributor
        self.statusBar().showMessage(f"Scanned {dc.describe()}", 8000)
        if not matches:
            ans = QMessageBox.question(
                self, "Distributor code",
                f"{dc.describe()}\n\nNo component matches. "
                "Create a new component?")
            if ans != QMessageBox.Yes:
                return
            sources = []
            if sup_id is not None and dc.part_number:
                sources.append({"sup_id": sup_id, "ordering_code": dc.part_number})
            elif sup_id is not None and dc.mpn:
                sources.append({"sup_id": sup_id, "ordering_code": dc.mpn})
            prefill = {"value": dc.mpn or dc.part_number,
                       "comment": dc.manufacturer, "sources": sources,
                       "quantity": dc.quantity or 0, "stock_comment": comment}
            self.on_new(prefill)
            return
        dev_id = matches[0]
        if len(matches) > 1:
            items = []
            for i in matches:
                d = self.db.device(i)
                items.append(f"#{i} {d['value'] or ''} {d['value2'] or ''} "
                             f"({d['category_name'] or ''})")
            item, ok = QInputDialog.getItem(self, "Distributor code",
                                            f"{dc.describe()}\nSeveral components match:",
                                            items, 0, False)
            if not ok:
                return
            dev_id = matches[items.index(item)]
        self.show_exact_id(dev_id)
        self.on_stock(True, qty=dc.quantity, comment=comment)

    def on_new(self, prefill=None):
        sel = self.tree.selectedItems()
        cid = sel[0].data(0, Qt.UserRole) if sel else None
        dlg = ComponentDialog(self, self.db, mode="new", category_id=cid, prefill=prefill)
        self._run_component_dialog(dlg)

    def on_edit(self):
        dev_id = self.need_selection()
        if dev_id is not None:
            self._run_component_dialog(ComponentDialog(self, self.db, dev_id, mode="edit"))

    def on_dup(self):
        dev_id = self.need_selection()
        if dev_id is not None:
            self._run_component_dialog(
                ComponentDialog(self, self.db, dev_id, mode="duplicate"))

    def _run_component_dialog(self, dlg):
        saved = dlg.exec()
        if dlg.category.changed:
            self.reload_categories()
        if saved and dlg.saved_id is not None:
            verb = {"new": "Added", "edit": "Saved", "duplicate": "Added copy"}[dlg.mode]
            if dlg.mode == "edit":
                self.after_stock_change(dlg.saved_id)
            else:
                self.show_exact_id(dlg.saved_id)
            self.statusBar().showMessage(f"{verb} component #{dlg.saved_id}", 5000)

    def on_delete(self):
        dev_id = self.need_selection()
        if dev_id is None:
            return
        dev = self.db.device(dev_id)
        usage = ", ".join(f"{n} {k}" for k, n in self.db.component_usage(dev_id).items() if n)
        ans = QMessageBox.question(
            self, "Delete component",
            f"Delete component #{dev_id} {dev['value'] or ''} {dev['value2'] or ''}?"
            + (f"\n\nThis also deletes its {usage}." if usage else "")
            + "\n\nKiCad schematics that use this id lose their link to the database.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ans != QMessageBox.Yes:
            return
        try:
            self.db.delete_component(dev_id)
        except sqlite3.Error as e:
            QMessageBox.critical(self, "Delete component", str(e))
            return
        self.refresh()
        self.detail.clear_device()
        self.statusBar().showMessage(f"Deleted component #{dev_id}", 5000)

    def on_suppliers(self):
        SupplierDialog(self, self.db).exec()
        dev_id = self.current_id()
        if dev_id is not None:
            self.detail.show_device(self.db, dev_id)

    def _need_history(self):
        if self.db.needs_migration():
            QMessageBox.information(
                self, "Stock", "The database is not upgraded yet, so there is no "
                "stock history. Use File → Reload to be asked again.")
            return False
        return True

    def on_undo(self):
        dev_id = self.need_selection()
        if dev_id is None or not self._need_history():
            return
        last = self.db.last_undoable(dev_id)
        if last is None:
            QMessageBox.information(self, "Undo", f"#{dev_id}: nothing to undo.")
            return
        if QMessageBox.question(
                self, "Undo",
                f"Undo the last stock change of #{dev_id}?\n\n"
                f"{last['timestamp']}: {last['delta']:+d} ({last['reason']}"
                + (f", {last['comment']}" if last["comment"] else "") + ")"
                ) != QMessageBox.Yes:
            return
        try:
            _, qty = self.db.undo_last(dev_id)
        except (ValueError, sqlite3.Error) as e:
            QMessageBox.warning(self, "Undo", str(e))
            return
        self.after_stock_change(dev_id)
        self.statusBar().showMessage(f"#{dev_id}: undone, quantity now {qty}", 5000)

    def on_history(self):
        if self._need_history():
            HistoryLogDialog(self, self.db).exec()

    def on_low_stock(self):
        LowStockDialog(self, self.db).exec()

    def on_bom(self):
        if not self._need_history():
            return
        if BomDialog(self, self.db).exec():
            self.refresh()
            dev_id = self.current_id()
            if dev_id is not None:
                self.detail.show_device(self.db, dev_id)

    def on_stock(self, receive, qty=None, comment=""):
        dev_id = self.need_selection()
        if dev_id is None:
            return
        if self.db.needs_migration():
            QMessageBox.information(
                self, "Stock", "The database is not upgraded yet, so stock changes "
                "cannot be logged. Use File → Reload to be asked again.")
            return
        stock = self.db.stock(dev_id)
        cur = (stock["quantity"] or 0) if stock else 0
        if not receive and cur <= 0:
            QMessageBox.information(self, "Consume stock", "Nothing in stock.")
            self.scan_focus()
            return
        dlg = StockDialog(self, self.db.device(dev_id), cur, receive, qty, comment)
        if dlg.exec():
            delta = dlg.qty.value() * (1 if receive else -1)
            try:
                qty = self.db.change_stock(dev_id, delta, dlg.reason.currentText(),
                                           dlg.comment.text().strip())
            except (ValueError, sqlite3.Error) as e:
                QMessageBox.critical(self, "Stock", str(e))
            else:
                self.after_stock_change(dev_id)
                self.statusBar().showMessage(f"#{dev_id}: {cur} → {qty}", 5000)
        self.scan_focus()

    def on_stock_settings(self):
        dev_id = self.need_selection()
        if dev_id is None:
            return
        if self.db.needs_migration():
            QMessageBox.information(
                self, "Stock", "The database is not upgraded yet. "
                "Use File → Reload to be asked again.")
            return
        dlg = StockSettingsDialog(self, self.db, self.db.device(dev_id),
                                  self.db.stock(dev_id))
        if dlg.exec():
            try:
                self.db.set_stock_settings(dev_id, dlg.warn.value(),
                                           dlg.location.text())
            except sqlite3.Error as e:
                QMessageBox.critical(self, "Stock settings", str(e))
                return
            self.after_stock_change(dev_id)

    def after_stock_change(self, dev_id):
        self.refresh()
        self.select_id(dev_id)
        self.detail.show_device(self.db, dev_id)

    def on_label_settings(self):
        # Preview the selected component, or the first one in the list.
        dev_id = self.current_id()
        if dev_id is None and self.model.rowCount():
            dev_id = self.model.item(0, 0).data(Qt.DisplayRole)
        if dev_id is None:
            dev_id = self.db.con.execute("SELECT MIN(id) FROM device").fetchone()[0]
        if dev_id is None:
            QMessageBox.information(self, "Label settings",
                                    "The database has no components to preview.")
            return
        LabelSettingsDialog(self, self.settings, self.db, dev_id).exec()

    def on_print(self):
        ids = self.selected_ids()
        if not ids:
            self.need_selection()
            return
        LabelDialog(self, self.db, ids).exec()


def recent_files(settings):
    value = settings.value("recent", [])
    if isinstance(value, str):  # QSettings returns a str for 1-item lists
        value = [value]
    return [p for p in (value or []) if os.path.exists(p)]


def remember_file(settings, path):
    path = os.path.abspath(path)
    recent = [path] + [p for p in recent_files(settings) if p != path]
    settings.setValue("recent", recent[:MAX_RECENT])


def main():
    app = QApplication(sys.argv)
    settings = QSettings("kicad_utils", "inventory_gui")

    # Database: command line > last used > ../db/db.sqlite > ask.
    candidates = sys.argv[1:2] or recent_files(settings)[:1] + [DEFAULT_DB]
    db = None
    for path in candidates:
        try:
            db = Database(path)
            break
        except (sqlite3.Error, ValueError) as e:
            print(f"cannot open {path}: {e}", file=sys.stderr)
    while db is None:
        path, _ = QFileDialog.getOpenFileName(
            None, "Open component database", "",
            "SQLite databases (*.sqlite *.sqlite3 *.db);;All files (*)")
        if not path:
            sys.exit(1)
        try:
            db = Database(path)
        except (sqlite3.Error, ValueError) as e:
            QMessageBox.critical(None, "Cannot open database", f"{path}\n\n{e}")

    win = MainWindow(db)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
