"""
分辨率选择对话框 - 通用版（支持 yt-dlp 所有平台）
"""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, 
    QPushButton, QTableWidget, QTableWidgetItem, QHeaderView
)
from PyQt6.QtCore import Qt
from utils.logger import logger


class FormatSelectionDialog(QDialog):
    """分辨率选择对话框"""
    
    def __init__(self, formats: list, parent=None):
        super().__init__(parent)
        self.formats = formats
        self.selected_format = None
        self._init_ui()
    
    def _init_ui(self):
        self.setWindowTitle("选择视频分辨率")
        self.setMinimumWidth(400)
        self.setMinimumHeight(300)
        
        layout = QVBoxLayout(self)
        
        # 标题
        title_label = QLabel("选择下载清晰度")
        title_label.setObjectName("section_title")
        layout.addWidget(title_label)

        intro_label = QLabel("优先展示 720p 及以上格式；如果没有可用项，可直接使用最佳质量。")
        intro_label.setObjectName("panel_intro")
        layout.addWidget(intro_label)
        
        # 格式表格 - 简化版，只显示基本信息
        self.format_table = QTableWidget()
        self.format_table.setColumnCount(5)
        self.format_table.setHorizontalHeaderLabels([
            "ID", "分辨率", "格式", "编码", "大小"
        ])
        
        # 设置列宽
        header = self.format_table.horizontalHeader()
        self.format_table.setColumnWidth(0, 60)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.format_table.setColumnWidth(2, 60)
        self.format_table.setColumnWidth(3, 70)
        self.format_table.setColumnWidth(4, 80)
        
        # 设置表格行为
        self.format_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.format_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.format_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.format_table.verticalHeader().setVisible(False)
        
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
            fps = fmt.get('fps', 30)
            if fps and fps > 30:
                resolution = f"{height}p{int(fps)}"
            else:
                resolution = f"{height}p"
            res_item = QTableWidgetItem(resolution)
            self.format_table.setItem(row, 1, res_item)
            
            # 格式
            ext = fmt.get('ext', '')
            ext_item = QTableWidgetItem(ext)
            self.format_table.setItem(row, 2, ext_item)
            
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
            self.format_table.setItem(row, 3, codec_item)
            
            # 大小
            size = fmt.get('filesize_str', '')
            size_item = QTableWidgetItem(size)
            self.format_table.setItem(row, 4, size_item)
        
        layout.addWidget(self.format_table)
        
        # 如果没有符合条件的格式
        if self.format_table.rowCount() == 0:
            no_format_label = QLabel("没有找到 720p 及以上格式，将回退到最佳质量。")
            no_format_label.setObjectName("hint_text")
            layout.addWidget(no_format_label)
        else:
            # 默认选中第一行
            self.format_table.selectRow(0)
        
        # 按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        # 最佳质量按钮
        best_btn = QPushButton("最佳质量")
        best_btn.setToolTip("自动选择最高画质")
        best_btn.setObjectName("secondary_button")
        best_btn.clicked.connect(self._on_best_clicked)
        btn_layout.addWidget(best_btn)
        
        # 确定按钮
        ok_btn = QPushButton("确认下载")
        ok_btn.setObjectName("success_button")
        ok_btn.clicked.connect(self._on_ok_clicked)
        btn_layout.addWidget(ok_btn)
        
        # 取消按钮
        cancel_btn = QPushButton("取消")
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
