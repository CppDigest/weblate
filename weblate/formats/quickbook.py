# Copyright © Boost Organization <boost@boost.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
QuickBook file format support for Weblate.

This format handles .qbk files for documentation translation using a
built-in pure-Python parser.  The parsing and PO conversion logic lives
in :mod:`weblate.utils.quickbook`.
"""

from __future__ import annotations

from pathlib import Path

from django.utils.translation import gettext_lazy

from translate.storage.pypo import pofile

from weblate.formats.convert import ConvertFormat
from weblate.utils.errors import report_error
from weblate.utils.quickbook import po_to_qbk, qbk_to_po


class QuickBookFormat(ConvertFormat):
    """QuickBook (.qbk) documentation file format with built-in PO converter.

    Uses a pure-Python parser to extract translatable strings (paragraphs,
    headings, sections, admonitions, list blocks, tables, variable lists) and
    to reconstruct translated ``.qbk`` files from PO translations.

    Registered via ``WEBLATE_FORMATS`` in ``weblate/formats/models.py``.
    """

    # Translators: File format name
    name = gettext_lazy("QuickBook file")
    autoload = ("*.qbk",)
    format_id = "quickbook"
    monolingual = True

    def convertfile(self, storefile, template_store):
        """Extract translatable strings from a .qbk file, returning a ``pofile``."""
        # Resolve the template (source-language) .qbk file path.
        template_path: str | None = None
        if template_store is not None and hasattr(template_store, "storefile"):
            tf = template_store.storefile
            if hasattr(tf, "name"):
                template_path = tf.name
            elif isinstance(tf, str):
                template_path = tf

        if template_path is None:
            # Fall back: use storefile itself as the template.
            if isinstance(storefile, str):
                template_path = storefile
            else:
                template_path = getattr(storefile, "name", None)

        if template_path is None:
            report_error("QuickBook: cannot determine template file path")
            empty = pofile()
            empty.updateheader(add=True, x_accelerator_marker=None, x_previous_msgid=None)
            return empty

        try:
            content = Path(template_path).read_text(encoding="utf-8")
        except Exception as exc:
            report_error(f"QuickBook: cannot read template {template_path}: {exc}")
            empty = pofile()
            empty.updateheader(add=True, x_accelerator_marker=None, x_previous_msgid=None)
            return empty

        filename = Path(template_path).name
        store = qbk_to_po(content, filename, self.existing_units)

        # When loading the source-language file (storefile IS the template), set
        # target = source on every unit.  This mirrors what po4a-gettextize produces
        # when given the same file for both master and localized, and is required so
        # that Weblate stores the correct (non-empty) translation for the source
        # language in a monolingual component.
        storefile_path = getattr(storefile, "name", storefile) if not isinstance(storefile, str) else storefile
        if storefile_path == template_path:
            for unit in store.units:
                if not unit.isheader():
                    unit.target = unit.source

        return store

    def save_content(self, handle) -> None:
        """Write the translated .qbk by applying PO translations to the template."""
        template_path = self.template_store.storefile
        if hasattr(template_path, "name"):
            template_path = template_path.name

        try:
            template_content = Path(template_path).read_text(encoding="utf-8")
        except Exception as exc:
            msg = f"QuickBook: cannot read template {template_path}: {exc}"
            report_error(msg)
            raise RuntimeError(msg) from exc

        filename = Path(template_path).name
        translated = po_to_qbk(template_content, self.store, filename)
        handle.write(translated.encode("utf-8"))

    @staticmethod
    def needs_target_sync(template_store) -> bool:
        """Copy source into target when loading the source-language template file."""
        return template_store is None

    @staticmethod
    def mimetype() -> str:
        """Return most common mime type for format."""
        return "text/x-quickbook"

    @staticmethod
    def extension() -> str:
        """Return most common file extension for format."""
        return "qbk"
