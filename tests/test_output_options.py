import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from PySide6.QtWidgets import QApplication
from ui.output_options import OutputOptions
from core.output_organization import make_values


class OutputOptionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_preview_uses_current_context(self):
        widget = OutputOptions()
        widget.set_context('G:/downloads', make_values('实际中文标题', site='Douyin', extension='mkv'))
        widget.template_edit.setText('{site}_{title}.{ext}')
        self.assertIn('Douyin_实际中文标题.mkv', widget.validate())
        self.assertEqual(widget.classification_combo.currentData(), 'none')
        widget.deleteLater()

    def test_bad_rule_is_visible(self):
        widget = OutputOptions({'classification':'custom','directory_template':'../escape'})
        self.assertIn('不允许', widget.preview.text())
        with self.assertRaises(ValueError):
            widget.validate()
        widget.deleteLater()
