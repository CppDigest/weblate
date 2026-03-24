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
from typing import IO, TYPE_CHECKING

from django.utils.translation import gettext_lazy
from translate.storage.pypo import pofile

from weblate.formats.convert import ConvertFormat
from weblate.utils.errors import report_error
from weblate.utils.quickbook import po_to_qbk, qbk_to_po

if TYPE_CHECKING:
    from translate.storage.base import TranslationStore

    from weblate.formats.base import TranslationFormat


class QuickBookFormat(ConvertFormat):
    """
    QuickBook (.qbk) documentation file format with built-in PO converter.

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

    def convertfile(
        self,
        storefile: IO[bytes],
        template_store: TranslationFormat | None,
    ) -> TranslationStore:
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
            # Fall back: use storefile path as the template.
            template_path = getattr(storefile, "name", None)

        if template_path is None:
            report_error("QuickBook: cannot determine template file path")
            empty = pofile()
            empty.updateheader(
                add=True, x_accelerator_marker=None, x_previous_msgid=None
            )
            return empty

        try:
            content = Path(template_path).read_text(encoding="utf-8")
        except Exception as exc:
            report_error(f"QuickBook: cannot read template {template_path}: {exc}")
            empty = pofile()
            empty.updateheader(
                add=True, x_accelerator_marker=None, x_previous_msgid=None
            )
            return empty

        filename = Path(template_path).name
        store = qbk_to_po(content, filename, self.existing_units)

        storefile_path: str | None = getattr(storefile, "name", None)
        if storefile_path == template_path:
            # Loading the source-language file: set target = source on every unit
            # so Weblate stores a non-empty translation for the source language.
            for unit in store.units:
                if not unit.isheader():
                    unit.target = unit.source
        # Loading a translated .qbk file: parse it and pair its segments
        # positionally with the template segments to populate msgstr values.
        # This mirrors what po4a-gettextize does when given both -m and -l.
        elif storefile_path is None:
            report_error(
                "QuickBook: cannot load translated .qbk without a filesystem path"
            )
        else:
            try:
                translated_content = Path(storefile_path).read_text(encoding="utf-8")
                translated_store = qbk_to_po(
                    translated_content, Path(storefile_path).name
                )
                trans_units = [u for u in translated_store.units if not u.isheader()]
                tmpl_units = [u for u in store.units if not u.isheader()]
                if len(tmpl_units) != len(trans_units):
                    report_error(
                        "QuickBook: refusing positional import: segment count mismatch "
                        f"(file={storefile_path!s}, name={Path(storefile_path).name!s}, "
                        f"template_units={len(tmpl_units)}, translated_units={len(trans_units)})"
                    )
                else:
                    for tmpl_unit, trans_unit in zip(
                        tmpl_units, trans_units, strict=True
                    ):
                        if trans_unit.source:
                            tmpl_unit.target = trans_unit.source
            except Exception as exc:
                report_error(
                    f"QuickBook: cannot read translated file {storefile_path}: {exc}"
                )

        return store

    def save_content(self, handle: IO[bytes]) -> None:
        """Write the translated .qbk by applying PO translations to the template."""
        template_store = getattr(self, "template_store", None)
        if template_store is None:
            msg = "QuickBook: cannot save: no template store"
            report_error(msg)
            raise RuntimeError(msg)

        storefile = getattr(template_store, "storefile", None)
        if storefile is None:
            msg = "QuickBook: cannot save: template store has no storefile"
            report_error(msg)
            raise RuntimeError(msg)

        template_path = (
            storefile.name
            if hasattr(storefile, "name")
            else storefile
            if isinstance(storefile, str)
            else None
        )
        if not template_path:
            msg = "QuickBook: cannot save: cannot determine template file path"
            report_error(msg)
            raise RuntimeError(msg)

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
    def needs_target_sync(template_store: TranslationFormat | None) -> bool:
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
