"""
Modern stylesheet for M3U8 Video Sniffer
"""

MODERN_STYLE = """
QWidget {
    background-color: #f6f4ef;
    color: #1f2933;
    font-family: "Microsoft YaHei UI", "Segoe UI", Arial, sans-serif;
    font-size: 10pt;
}

QMainWindow {
    background-color: #f3f0e8;
}

QFrame[objectName="hero_card"],
QFrame[objectName="toolbar_card"],
QFrame[objectName="panel_card"],
QGroupBox {
    background-color: #fbfaf6;
    border: 1px solid #e7e0d3;
    border-radius: 12px;
}

QFrame[objectName="hero_card"] {
    background-color: #fbfaf6;
}

QFrame[objectName="toolbar_card"] {
    padding: 0;
}

QFrame[objectName="panel_card"] {
    padding: 0;
}

QPushButton {
    background-color: #22577a;
    color: #ffffff;
    border: none;
    border-radius: 8px;
    padding: 7px 14px;
    font-weight: 600;
    min-height: 22px;
}

QPushButton:hover {
    background-color: #173f59;
}

QPushButton:pressed {
    background-color: #123347;
}

QPushButton:disabled {
    background-color: #b8b4ac;
    color: #f6f4f0;
}

QPushButton:checked {
    background-color: #173f59;
}

QPushButton[objectName^="nav_"] {
    background-color: #faf7f1;
    border: 1px solid #e3dccf;
    border-radius: 10px;
    padding: 2px;
    min-width: 24px;
    min-height: 24px;
    max-width: 24px;
    max-height: 24px;
}

QPushButton[objectName^="nav_"]:hover {
    background-color: #ece3d5;
    border-color: #c4ae8a;
}

QPushButton[objectName="secondary_button"] {
 background-color: #e7ddd0;
 color: #30404d;
}

QPushButton[objectName="secondary_button"]:hover {
 background-color: #dccfbe;
}

QPushButton[objectName="secondary_button"]:pressed {
 background-color: #d1c0aa;
}

QPushButton[objectName="queue_pause_button"] {
 background-color: #f2cc8f;
 color: #3a2f1d;
}

QPushButton[objectName="queue_pause_button"]:hover {
 background-color: #e8bd73;
}

QPushButton[objectName="queue_pause_button"]:pressed {
 background-color: #d9ab55;
}

QPushButton[objectName="queue_resume_button"] {
 background-color: #81b29a;
}

QPushButton[objectName="queue_resume_button"]:hover {
 background-color: #6aa186;
}

QPushButton[objectName="queue_resume_button"]:pressed {
 background-color: #578972;
}

QPushButton[objectName="queue_stop_button"] {
 background-color: #e07a5f;
}

QPushButton[objectName="queue_stop_button"]:hover {
 background-color: #d06a51;
}

QPushButton[objectName="queue_stop_button"]:pressed {
 background-color: #b9583f;
}

QPushButton[objectName="queue_delete_button"] {
 background-color: #c9494a;
}

QPushButton[objectName="queue_delete_button"]:hover {
 background-color: #b33b3c;
}

QPushButton[objectName="queue_delete_button"]:pressed {
 background-color: #9b2f30;
}

QPushButton[objectName="queue_retry_button"] {
 background-color: #5f8cc0;
}

QPushButton[objectName="queue_retry_button"]:hover {
 background-color: #4f7db0;
}

QPushButton[objectName="queue_retry_button"]:pressed {
 background-color: #3f6b9c;
}

QPushButton[objectName="queue_open_button"] {
 background-color: #6d8fbb;
}

QPushButton[objectName="queue_open_button"]:hover {
 background-color: #5b7da7;
}

QPushButton[objectName="queue_open_button"]:pressed {
 background-color: #4b6a91;
}

QPushButton[objectName="queue_pauseall_button"],
QPushButton[objectName="queue_clear_button"],
QPushButton[objectName="queue_sort_button"],
QPushButton[objectName="queue_import_button"] {
 background-color: #7b6f90;
}

QPushButton[objectName="queue_pauseall_button"]:hover,
QPushButton[objectName="queue_clear_button"]:hover,
QPushButton[objectName="queue_sort_button"]:hover,
QPushButton[objectName="queue_import_button"]:hover {
 background-color: #6a5d83;
}

QPushButton[objectName="queue_pauseall_button"]:pressed,
QPushButton[objectName="queue_clear_button"]:pressed,
QPushButton[objectName="queue_sort_button"]:pressed,
QPushButton[objectName="queue_import_button"]:pressed {
 background-color: #584c74;
}

QPushButton[objectName="danger_button"] {
    background-color: #b85c38;
}

QPushButton[objectName="danger_button"]:hover {
    background-color: #9f4d2d;
}

QPushButton[objectName="danger_button"]:pressed {
    background-color: #843f23;
}

QPushButton[objectName="success_button"] {
    background-color: #2f7d60;
}

QTableWidget QPushButton[objectName="success_button"] {
    padding: 5px 10px;
    min-height: 18px;
    font-size: 9pt;
}

QPushButton[objectName="success_button"]:hover {
    background-color: #25654e;
}

QPushButton[objectName="success_button"]:pressed {
    background-color: #1f5340;
}

QLineEdit,
QTextEdit,
QComboBox,
QSpinBox,
QTableWidget,
QTreeWidget {
    background-color: #fffdf9;
    border: 1px solid #e4ddd0;
    border-radius: 8px;
    selection-background-color: #22577a;
    selection-color: #ffffff;
}

/* 右上角入口统一：扁平化链接/按钮风格 */
#language_button,
#manual_link {
    color: #1a3a5f;
    font-weight: 500;
    font-size: 10pt;
    padding: 2px 10px; /* 减小上下 padding，增加左右 padding */
    background: transparent;
    border: none;
    border-radius: 4px;
}

/* 彻底禁用导致变形的默认下拉箭头 */
#language_button::menu-indicator {
    image: none;
    width: 0px;
}

#language_button:hover,
#manual_link:hover {
    color: #2a5a8f;
    background-color: rgba(0, 0, 0, 0.05);
}

/* 弹出菜单外观美化 */
QMenu#language_menu {
    background-color: #ffffff;
    border: 1px solid #e1e4e8;
    border-radius: 6px;
    padding: 4px;
}

QMenu#language_menu::item {
    padding: 6px 24px;
    border-radius: 4px;
    color: #24292e;
}

QMenu#language_menu::item:selected {
    background-color: #f6f8fa;
    color: #0366d6;
}

QLineEdit,
QComboBox,
QSpinBox {
    padding: 6px 12px;
    min-height: 30px;
}

/* 下拉列表整体样式 */
QComboBox QAbstractItemView {
    background-color: #fffdf9;
    border: 1px solid #e4ddd0;
    border-radius: 8px;
    padding: 2px; /* 减小列表整体边距 */
    outline: 0px;
}

/* 下拉列表中的每一项 */
QComboBox QAbstractItemView::item {
    padding: 6px 12px;  /* 减小上下间距（从10px降至6px） */
    border-radius: 6px;
    margin: 1px 4px;    /* 减小项间距（从2px降至1px） */
    min-height: 22px;   /* 减小最小高度 */
}

QComboBox QAbstractItemView::item:selected {
    background-color: #22577a;
    color: #ffffff;
}

QLineEdit:focus,
QTextEdit:focus,
QComboBox:focus,
QSpinBox:focus {
    border: 1px solid #22577a;
}

QTextEdit {
    padding: 8px 10px;
}

QTextEdit[objectName="download_center_log_text"] {
    font-size: 9pt;
}

QComboBox:hover,
QSpinBox:hover {
    border-color: #b77943;
}

QComboBox::drop-down,
QSpinBox::up-button,
QSpinBox::down-button {
    border: none;
    background: transparent;
    width: 24px;
}

QSpinBox::up-button,
QSpinBox::down-button {
    subcontrol-origin: border;
    background-color: transparent;
}

QSpinBox::up-button {
    subcontrol-position: top right;
    height: 15px;
    margin-top: 3px;
    margin-right: 4px;
}

QSpinBox::down-button {
    subcontrol-position: bottom right;
    height: 15px;
    margin-bottom: 3px;
    margin-right: 4px;
}

QComboBox::down-arrow {
    image: url(resources/dropdown_arrow.svg);
    width: 10px;
    height: 6px;
}

QSpinBox::up-arrow {
    image: url(resources/spin_up_arrow.svg);
    width: 8px;
    height: 5px;
}

QSpinBox::down-arrow {
    image: url(resources/spin_down_arrow.svg);
    width: 8px;
    height: 5px;
}

QComboBox QAbstractItemView {
    background-color: #fffdf9;
    border: 1px solid #d8cab4;
    selection-background-color: #22577a;
    selection-color: #ffffff;
    outline: none;
}

QTabWidget::pane {
    border: none;
    background: transparent;
}

QTabBar::tab {
    background-color: transparent;
    color: #54606c;
    border: 1px solid transparent;
    border-bottom: none;
    border-top-left-radius: 10px;
    border-top-right-radius: 10px;
    padding: 9px 16px;
    margin-right: 4px;
    font-weight: 600;
}

QTabBar::tab:selected {
    background-color: #fbfaf6;
    color: #17324d;
    border-color: #e7e0d3;
}

QTabBar::tab:hover:!selected {
    background-color: #f3efe7;
}

QHeaderView::section {
    background-color: #f8f5ef;
    color: #5c6773;
    padding: 8px 8px;
    border: none;
    border-bottom: 1px solid #ece5d8;
    font-weight: 700;
}

QTableWidget,
QTreeWidget {
    gridline-color: #f4eee4;
    outline: none;
}

QTableWidget::item,
QTreeWidget::item {
    padding: 8px 10px;
    border: none;
}

QTableWidget::item:selected,
QTreeWidget::item:selected {
    background-color: #dbe7ef;
    color: #17324d;
}

QTreeWidget::item {
    border-bottom: 1px solid #f7f3eb;
}

QLabel {
    color: #23313d;
    background: transparent;
}

QLabel[objectName="hero_title"] {
    color: #17324d;
    font-size: 15pt;
    font-weight: 700;
}

QLabel[objectName="hero_subtitle"] {
    color: #7a7267;
    font-size: 9pt;
}

QLabel[objectName="hero_badge"] {
    color: #6b6256;
    background-color: transparent;
    padding: 0;
    font-weight: 600;
}

QLabel[objectName="section_title"] {
    font-size: 11.5pt;
    font-weight: 700;
}

QLabel[objectName="panel_intro"] {
    color: #6f6559;
    font-size: 9pt;
}

QLabel[objectName="page_title"] {
    font-size: 15pt;
    font-weight: 800;
}

QLabel[objectName="muted_text"],
QLabel[objectName="hint_text"] {
    color: #786f64;
}

QLabel[objectName="hint_text"] {
    font-size: 9pt;
}

QLabel[objectName="path_display"] {
    background-color: #faf7f1;
    border: 1px solid #e4ddd0;
    border-radius: 8px;
    padding: 7px 10px;
    color: #4e5965;
}

QLabel[objectName="manual_link"] {
    color: #17324d;
    font-weight: 700;
}

QLabel[objectName="manual_link"]:hover {
    color: #b77943;
}

QLabel[objectName="log_label"] {
    color: #1f2933;
    font-weight: 600;
}

QLabel[objectName="status_chip"] {
    color: #7a7267;
    border: none;
    padding: 0;
    font-weight: 500;
}

QFrame[objectName="card"] {
    background-color: #fffaf2;
    border: 1px solid #e7e0d3;
    border-radius: 12px;
}

QFrame[objectName="dark_card"] {
    background-color: #ffffff;
    border: 1px solid #d9dee5;
    border-radius: 12px;
}

QFrame[objectName="divider"] {
    background-color: transparent;
    min-height: 1px;
    max-height: 1px;
    border: none;
}

QGroupBox {
    margin-top: 10px;
    padding-top: 16px;
    font-weight: 700;
}

QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 14px;
    padding: 0 8px;
    color: #17324d;
}

QSplitter::handle {
    background-color: #ebe4d8;
    width: 3px;
    height: 3px;
    border-radius: 2px;
}

QSplitter::handle:hover {
    background-color: #b77943;
}

QScrollBar:vertical {
    background-color: #efe7da;
    width: 12px;
    border-radius: 6px;
}

QScrollBar:horizontal {
    background-color: #efe7da;
    height: 12px;
    border-radius: 6px;
}

QScrollBar::handle:vertical,
QScrollBar::handle:horizontal {
    background-color: #c8b8a2;
    border-radius: 6px;
    min-height: 28px;
    min-width: 28px;
}

QScrollBar::handle:vertical:hover,
QScrollBar::handle:horizontal:hover {
    background-color: #ad9578;
}

QScrollBar::add-line,
QScrollBar::sub-line {
    border: none;
    background: none;
}

QToolTip {
    background-color: #17324d;
    color: #fff9f0;
    border: none;
    border-radius: 8px;
    padding: 6px 10px;
}

QProgressBar {
    border: 1px solid #d8cab4;
    border-radius: 9px;
    background-color: #efe7da;
    text-align: center;
    height: 18px;
}

QProgressBar::chunk {
    background-color: #2f7d60;
    border-radius: 8px;
}

QTextEdit[objectName="log_area"] {
    background-color: transparent;
    color: #1f2933;
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", Consolas, "Courier New", monospace;
    font-size: 9pt;
    border: none;
    border-radius: 0px;
    padding: 4px 6px;
}
"""
