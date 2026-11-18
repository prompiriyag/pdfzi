"""Screens, in the order the user sees them: menu, files, (order), options, run, result."""

from pdfzi.tui.screens.help import HelpScreen
from pdfzi.tui.screens.menu import MenuScreen
from pdfzi.tui.screens.options import OptionsScreen
from pdfzi.tui.screens.order import OrderScreen
from pdfzi.tui.screens.picker import PickerScreen
from pdfzi.tui.screens.result import Outcome, ResultScreen
from pdfzi.tui.screens.run import RunScreen

__all__ = [
    "HelpScreen",
    "MenuScreen",
    "OptionsScreen",
    "OrderScreen",
    "Outcome",
    "PickerScreen",
    "ResultScreen",
    "RunScreen",
]
