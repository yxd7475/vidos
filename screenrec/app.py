"""应用入口。"""
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from screenrec.ui.main_window import MainWindow


def main() -> None:
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("ScreenRec")
    app.setApplicationDisplayName("ScreenRec")

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
