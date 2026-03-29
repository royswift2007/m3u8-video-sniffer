"""
分辨率选择对话框 - 通用版（支持 yt-dlp 所有平台）
"""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, 
    QPushButton, QTableWidget, QTableWidgetItem, QHeaderView
)
from PyQt6.QtCore import Qt
from utils.logger import logger
from utils.i18n import TR


class FormatSelectionDialog(QDialog):
    """分辨率选择对话框"""
    
    def __init__(self, formats: list, parent=None):
        super().__init__(parent)
        self.formats = formats
        self.selected_format = None
        self._init_ui()
    
    def _init_ui(self):
        self.setWindowTitle(TR("dialog_select_resolution"))
        self.setMinimumWidth(400)
        self.setMinimumHeight(300)
        self.resize(600, 380)
        
        layout = QVBoxLayout(self)
        
        # 标题
        title_label = QLabel(TR("label_select_quality"))
        title_label.setObjectName("section_title")
        layout.addWidget(title_label)

        intro_label = QLabel(TR("hint_format_intro"))
        intro_label.setObjectName("panel_intro")
        layout.addWidget(intro_label)
        
        # 格式表格 - 简化版，只显示基本信息
        self.format_table = QTableWidget()
        self.format_table.setColumnCount(6)
        self.format_table.setHorizontalHeaderLabels([
            TR("col_format_id"), TR("col_format_resolution"), TR("col_format_fps"),
            TR("col_format_ext"), TR("col_format_codec"), TR("col_format_size")
        ])
        
        # 设置列宽：收窄"分辨率"列，并将剩余空间优先分配给"大小"列
        header = self.format_table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
            header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
            header.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
            header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.format_table.setColumnWidth(0, 68)
        self.format_table.setColumnWidth(1, 88)
        self.format_table.setColumnWidth(2, 64)
        self.format_table.setColumnWidth(3, 68)
        self.format_table.setColumnWidth(4, 84)
        
        # 设置表格行为
        self.format_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.format_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.format_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        vertical_header = self.format_table.verticalHeader()
        if vertical_header is not None:
            vertical_header.setVisible(False)
        
        # 填充数据 - 只显示 >= 720p 的格式
        filtered_formats = [f for f in self.formats if f.get('height', 0) >= 720]
        
        # 按分辨率降序排列
        filtered_formats.sort(key=lambda x: (x.get('height', 0), x.get('fps', 30)), reverse=True)
        
        for fmt in filtered_formats:
            row = self.format_table.rowCount()
            self.format_table.insertRow(row)
            
            # ID
            format_id = fmt.get('format_id', '')
            id_item = QTableWidgetItem(format_id)
            id_item.setData(Qt.ItemDataRole.UserRole, fmt)
            self.format_table.setItem(row, 0, id_item)
            
            # 分辨率
            height = fmt.get('height', 0)
            fps = fmt.get('fps')
            fps_display = '' if fps in (None, '') else str(int(fps)) if isinstance(fps, (int, float)) and float(fps).is_integer() else str(fps)
            if fps and fps > 30:
                resolution = f"{height}p{int(fps)}"
            else:
                resolution = f"{height}p"
            res_item = QTableWidgetItem(resolution)
            self.format_table.setItem(row, 1, res_item)

            # 帧率
            fps_item = QTableWidgetItem(fps_display)
            self.format_table.setItem(row, 2, fps_item)
            
            # 格式
            ext = fmt.get('ext', '')
            ext_item = QTableWidgetItem(ext)
            self.format_table.setItem(row, 3, ext_item)
            
            # 编码
            vcodec = fmt.get('vcodec', '')
            # 简化编码显示
            if 'avc1' in vcodec or 'h264' in vcodec.lower():
                vcodec = 'H.264'
            elif 'vp9' in vcodec or 'vp09' in vcodec:
                vcodec = 'VP9'
            elif 'av01' in vcodec:
                vcodec = 'AV1'
            elif 'hevc' in vcodec.lower() or 'hvc1' in vcodec:
                vcodec = 'H.265'
            codec_item = QTableWidgetItem(vcodec)
            self.format_table.setItem(row, 4, codec_item)
            
            # 大小
            size = fmt.get('filesize_str', '')
            size_item = QTableWidgetItem(size)
            self.format_table.setItem(row, 5, size_item)
        
        layout.addWidget(self.format_table)
        
        # 如果没有符合条件的格式
        if self.format_table.rowCount() == 0:
            no_format_label = QLabel(TR("hint_no_720p"))
            no_format_label.setObjectName("hint_text")
            layout.addWidget(no_format_label)
        else:
            # 默认选中第一行
            self.format_table.selectRow(0)
        
        # 按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        # 最佳质量按钮
        best_btn = QPushButton(TR("btn_best_quality"))
        best_btn.setToolTip(TR("tooltip_best_quality"))
        best_btn.setObjectName("secondary_button")
        best_btn.clicked.connect(self._on_best_clicked)
        btn_layout.addWidget(best_btn)
        
        # 确定按钮
        ok_btn = QPushButton(TR("btn_confirm_download"))
        ok_btn.setObjectName("success_button")
        ok_btn.clicked.connect(self._on_ok_clicked)
        btn_layout.addWidget(ok_btn)
        
        # 取消按钮
        cancel_btn = QPushButton(TR("btn_cancel"))
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        
        layout.addLayout(btn_layout)
        
        # 双击选择
        self.format_table.doubleClicked.connect(self._on_ok_clicked)
    
    def _on_best_clicked(self):
        """选择最佳质量"""
        self.selected_format = None
        self.accept()
    
    def _on_ok_clicked(self):
        """确定选择"""
        selected_rows = self.format_table.selectedIndexes()
        if selected_rows:
            row = selected_rows[0].row()
            item = self.format_table.item(row, 0)
            if item:
                self.selected_format = item.data(Qt.ItemDataRole.UserRole)
        self.accept()
    
    def get_selected_format(self):
        """获取选中的格式"""
        return self.selected_format
