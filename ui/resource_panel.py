"""
Resource panel for displaying detected video resources
"""
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QPushButton,
                             QHBoxLayout, QTableWidget, QTableWidgetItem, QHeaderView,
                             QLineEdit, QComboBox, QFrame)
from PyQt6.QtCore import pyqtSignal, Qt
from core.task_model import M3U8Resource
from core.m3u8_parser import M3U8FetchThread
from utils.logger import logger
from utils.i18n import i18n, TR


class ResourcePanel(QWidget):
    """资源面板 - 显示检测到的视频资源（表格布局）"""
    
    # 信号：用户点击下载
    download_requested = pyqtSignal(M3U8Resource)  # resource only, engine determined by main window
    
    def __init__(self):
        super().__init__()
        self.resources = []  # 保存资源对象列表
        self._seen_keys = set()  # 用于智能去重
        self._page_url_map = {}  # page_url -> row_index 映射
        self._m3u8_parse_threads = []  # 保持后台解析线程引用，防止 GC
        from utils.config_manager import config
        self._features = config.get("features", {}) or {}
        self._init_ui()
        self.retranslate_ui()
        
    def _on_download_btn_clicked(self, row_index: int):
        """处理下载按钮点击"""
        logger.info(
            "[UI] 点击资源下载按钮",
            row_index=row_index,
            table_rows=self.resource_table.rowCount(),
            resource_count=len(self.resources),
        )
        if 0 <= row_index < len(self.resources):
            resource, engine_name = self.resources[row_index]
            logger.info(
                "[UI] 发送下载请求",
                row_index=row_index,
                title=getattr(resource, "title", ""),
                url=getattr(resource, "url", ""),
                engine_name=engine_name,
                is_variant=getattr(resource, "is_variant", False),
            )
            # 只发送 resource，engine 由主窗口根据当前下拉选择决定
            self.download_requested.emit(resource)
        else:
            logger.error(
                "[UI] 下载按钮行号失效，已拦截",
                row_index=row_index,
                table_rows=self.resource_table.rowCount(),
                resource_count=len(self.resources),
            )
    
    def _init_ui(self):
        """初始化 UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        panel_card = QFrame()
        panel_card.setObjectName("panel_card")
        panel_layout = QVBoxLayout(panel_card)
        panel_layout.setContentsMargins(12, 12, 12, 12)
        panel_layout.setSpacing(8)

        header_layout = QHBoxLayout()

        title_block = QVBoxLayout()
        title_block.setSpacing(2)

        self.title_label = QLabel("")
        self.title_label.setObjectName("section_title")
        title_block.addWidget(self.title_label)

        self.intro_label = QLabel("")
        self.intro_label.setObjectName("panel_intro")
        title_block.addWidget(self.intro_label)

        header_layout.addLayout(title_block)
        
        header_layout.addStretch()
        
        # 批量操作按钮
        self.batch_download_btn = QPushButton("")
        self.batch_download_btn.setMinimumWidth(100)
        self.batch_download_btn.setObjectName("success_button")
        self.batch_download_btn.clicked.connect(self._on_batch_download_clicked)
        self.batch_download_btn.setVisible(self._features.get("ui_batch_actions", True))
        header_layout.addWidget(self.batch_download_btn)

        self.batch_remove_btn = QPushButton("")
        self.batch_remove_btn.setMinimumWidth(100)
        self.batch_remove_btn.setObjectName("secondary_button")
        self.batch_remove_btn.clicked.connect(self._on_batch_remove_clicked)
        self.batch_remove_btn.setVisible(self._features.get("ui_batch_actions", True))
        header_layout.addWidget(self.batch_remove_btn)
        
        # 清空按钮
        self.clear_btn = QPushButton("")
        self.clear_btn.setMinimumWidth(100)
        self.clear_btn.setObjectName("danger_button")
        self.clear_btn.clicked.connect(self._on_clear_clicked)
        header_layout.addWidget(self.clear_btn)
        
        panel_layout.addLayout(header_layout)

        # 搜索与过滤
        filter_layout = QHBoxLayout()
        filter_layout.setSpacing(8)

        self.search_input = QLineEdit()
        self.search_input.textChanged.connect(self._apply_filters)
        self.search_input.setVisible(self._features.get("ui_filter_search", True))
        filter_layout.addWidget(self.search_input)

        self.type_filter = QComboBox()
        self.type_filter.setMinimumWidth(140)
        self.type_filter.currentIndexChanged.connect(self._apply_filters)
        self.type_filter.setVisible(self._features.get("ui_filter_search", True))
        filter_layout.addWidget(self.type_filter)

        self.source_filter = QComboBox()
        self.source_filter.setMinimumWidth(140)
        self.source_filter.currentIndexChanged.connect(self._apply_filters)
        self.source_filter.setVisible(self._features.get("ui_filter_search", True))
        filter_layout.addWidget(self.source_filter)

        self.quality_filter = QComboBox()
        self.quality_filter.setMinimumWidth(140)
        self.quality_filter.currentIndexChanged.connect(self._apply_filters)
        self.quality_filter.setVisible(self._features.get("ui_filter_search", True))
        filter_layout.addWidget(self.quality_filter)

        filter_layout.addStretch()
        panel_layout.addLayout(filter_layout)
        
        # **资源表格** - 简洁布局
        self.resource_table = QTableWidget()
        self.resource_table.setColumnCount(7)
        
        # 设置列宽：主信息列拉伸，辅助信息列按内容收紧，减少右侧留白
        header = self.resource_table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)           # 文件名
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)  # 类型
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)  # 清晰度
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)           # 来源域名
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)  # 引擎
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)  # 时间
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)             # 操作
        
        # 表头样式交由全局样式统一管理
        
        # 设置基础宽度，Stretch 列会在此基础上按可用空间继续分配
        self.resource_table.setColumnWidth(0, 360)  # 文件名
        self.resource_table.setColumnWidth(1, 120)  # 类型
        self.resource_table.setColumnWidth(2, 88)   # 清晰度
        self.resource_table.setColumnWidth(3, 180)  # 来源域名
        self.resource_table.setColumnWidth(4, 130)  # 引擎
        self.resource_table.setColumnWidth(5, 110)  # 时间
        self.resource_table.setColumnWidth(6, 120)  # 操作
        
        # 设置行为
        self.resource_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        if self._features.get("ui_batch_actions", True):
            self.resource_table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        else:
            self.resource_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.resource_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.resource_table.verticalHeader().setVisible(False)  # 隐藏行号
        
        # 设置行高
        self.resource_table.verticalHeader().setDefaultSectionSize(40)
        
        # 使用全局样式控制表格边框
        self.resource_table.setFrameShape(QTableWidget.Shape.NoFrame)
        
        panel_layout.addWidget(self.resource_table)
        layout.addWidget(panel_card)

    def retranslate_ui(self):
        """刷新界面语言"""
        self.title_label.setText(TR("title_found_resources"))
        self.intro_label.setText(TR("intro_resource_panel"))
        
        self.batch_download_btn.setText(TR("btn_download_selected"))
        self.batch_remove_btn.setText(TR("btn_remove_selected"))
        self.clear_btn.setText(TR("btn_clear_list"))
        
        self.search_input.setPlaceholderText(TR("placeholder_search_resource"))
        
        # 过滤器选项 - 阻止信号以避免切换语言时 _apply_filters 被中间状态触发
        self.type_filter.blockSignals(True)
        self.source_filter.blockSignals(True)
        self.quality_filter.blockSignals(True)
        
        type_idx = self.type_filter.currentIndex()
        self.type_filter.clear()
        self.type_filter.addItems([
            TR("filter_all_types"), "M3U8", "MPD", "MP4", "FLV", "MKV", "WEBM", "TS", 
            TR("type_video_stream"), TR("type_playlist"), TR("type_unknown")
        ])
        self.type_filter.setCurrentIndex(max(0, type_idx))
        
        source_idx = self.source_filter.currentIndex()
        sources = [self.source_filter.itemText(i) for i in range(self.source_filter.count())]
        self.source_filter.clear()
        self.source_filter.addItem(TR("filter_all_sources"))
        if len(sources) > 1:
            self.source_filter.addItems(sources[1:])
        self.source_filter.setCurrentIndex(max(0, source_idx))
        
        quality_idx = self.quality_filter.currentIndex()
        self.quality_filter.clear()
        self.quality_filter.addItems([
            TR("filter_all_qualities"), "2160", "1440", "1080", "720", "480", "360", "240", TR("quality_audio")
        ])
        self.quality_filter.setCurrentIndex(max(0, quality_idx))
        
        self.type_filter.blockSignals(False)
        self.source_filter.blockSignals(False)
        self.quality_filter.blockSignals(False)
        
        # 表格表头
        self.resource_table.setHorizontalHeaderLabels([
            TR("col_filename"), TR("col_type"), TR("col_quality"), 
            TR("col_source"), TR("col_engine_suggest"), TR("col_time"), TR("col_actions")
        ])
        
        # 刷新表格中的动态内容（如操作按钮文字）
        for row in range(self.resource_table.rowCount()):
            btn = self.resource_table.cellWidget(row, 6)
            if isinstance(btn, QPushButton):
                btn.setText(TR("btn_download"))
                
            # 翻译类型单元格（如果是“未知”等可翻译文本）
            type_item = self.resource_table.item(row, 1)
            if type_item:
                raw_type = type_item.data(Qt.ItemDataRole.UserRole) or type_item.text()
                if raw_type in ("Unknown", "Video Stream", "Playlist"):
                    translated = TR(f"type_{raw_type.lower().replace(' ', '_')}")
                    type_item.setText(translated)
                    type_item.setData(Qt.ItemDataRole.UserRole, raw_type)
    
    def add_resource(self, resource: M3U8Resource, engine_name: str):
        """添加检测到的资源"""
        # 智能去重：生成标准化的去重键
        dedup_key = self._generate_dedup_key(resource)
        if self._features.get("sniffer_dedup_enabled", True):
            if dedup_key in self._seen_keys:
                return  # 跳过完全重复的资源
        
        # 通用视频平台：基于标题去重（处理同一视频的多个 URL 来源）
        url_str = str(resource.url).lower()
        page_url_str = str(resource.page_url).lower() if resource.page_url else ""
        combined_url = url_str + page_url_str
        
        # 主流视频平台 CDN 和页面域名 -> 平台名称和标题后缀
        platform_patterns = {
            # YouTube
            ('youtube.com', 'youtu.be', 'googlevideo.com'): (' - YouTube', 'youtube'),
            # Bilibili
            ('bilibili.com', 'bilivideo.com', 'b23.tv'): ('_哔哩哔哩_bilibili', 'bilibili'),
            # TikTok / 抖音
            ('tiktok.com', 'tiktokcdn.com'): (' | TikTok', 'tiktok'),
            ('douyin.com', 'douyincdn.com'): ('', 'douyin'),
            # Twitter / X
            ('twitter.com', 'x.com', 'twimg.com'): ('', 'twitter'),
            # Instagram
            ('instagram.com', 'cdninstagram.com'): ('', 'instagram'),
            # Vimeo
            ('vimeo.com', 'vimeocdn.com'): (' on Vimeo', 'vimeo'),
            # Twitch
            ('twitch.tv', 'twitchcdn.net'): (' - Twitch', 'twitch'),
            # 西瓜视频
            ('ixigua.com',): ('', 'ixigua'),
            # 优酷
            ('youku.com', 'ykimg.com'): ('', 'youku'),
            # 爱奇艺
            ('iqiyi.com', 'iqiyipic.com'): ('', 'iqiyi'),
            # 腾讯视频
            ('v.qq.com', 'gtimg.cn'): ('', 'qqvideo'),
            # Facebook
            ('facebook.com', 'fbcdn.net'): ('', 'facebook'),
            # Dailymotion
            ('dailymotion.com',): (' - Dailymotion', 'dailymotion'),
        }
        
        matched_platform = None
        title_suffix = ''
        
        for domains, (suffix, platform_name) in platform_patterns.items():
            if any(domain in combined_url for domain in domains):
                matched_platform = platform_name
                title_suffix = suffix
                break
        
        if matched_platform and resource.title and not getattr(resource, "is_variant", False):
            # 清理标题用于去重比较
            clean_title = resource.title
            if title_suffix:
                clean_title = clean_title.replace(title_suffix, "")
            clean_title = clean_title.strip()
            
            if clean_title:  # 确保清理后的标题不为空
                title_key = f"{matched_platform}_title:{clean_title}"
                
                if self._features.get("sniffer_dedup_enabled", True):
                    if title_key in self._seen_keys:
                        logger.debug(f"{matched_platform} 标题已存在，跳过: {clean_title}")
                        return  # 同标题的资源已存在
                
                self._seen_keys.add(title_key)
        
        # 特别针对 YouTube page_url 去重：如果 page_url 相同，可能是重复，但需要检查标题
        if not getattr(resource, "is_variant", False) and hasattr(resource, 'page_url') and resource.page_url and \
           ('youtube.com' in str(resource.page_url) or 'youtu.be' in str(resource.page_url)):
             
             page_key = f"page_url_dedup:{resource.page_url}"
             
             if page_key in self._page_url_map:
                 # 已存在相同 page_url 的资源
                 existing_row = self._page_url_map[page_key]
                 
                 # 获取现有行的标题
                 try:
                     current_title_item = self.resource_table.item(existing_row, 0)
                     current_title = current_title_item.text().replace("📹 ", "") if current_title_item else ""
                     new_title = resource.title.replace(" - YouTube", "").strip()
                     old_title = current_title.replace(" - YouTube", "").strip()
                     
                     # 检查标题是否真的相同（忽略 "YouTube" 通用标题）
                     is_current_generic = old_title == "YouTube" or "YouTube Video [" in old_title
                     is_new_generic = new_title == "YouTube" or "YouTube Video [" in new_title
                     
                     # 如果两个标题明显不同（都不是通用标题且内容不同），则这是新资源
                     # 这种情况发生在 SPA 导航时 page_url 还未更新但视频已切换
                     if not is_current_generic and not is_new_generic and old_title != new_title:
                         # 标题不同，不要跳过，让它作为新资源添加
                         logger.debug(f"YouTube 标题不同，作为新资源添加: {new_title}")
                         pass  # 继续执行，不 return
                     elif is_current_generic and not is_new_generic:
                         logger.info(f"更新 YouTube 资源标题: {current_title} -> {resource.title}")
                         # 更新表格中的标题
                         if current_title_item:
                             current_title_item.setText(f"📹 {resource.title}")
                             current_title_item.setToolTip(resource.title)
                             
                         # 更新内部存储的资源对象
                         original_resource, original_engine = self.resources[existing_row]
                         original_resource.title = resource.title
                         return  # 已更新，不需要添加新行
                     else:
                         # 标题相同或都是通用标题，跳过重复
                         if self._features.get("sniffer_dedup_enabled", True):
                             return
                 except Exception as e:
                     logger.warning(f"检查资源标题失败: {e}")
             
             # 记录 page_url 对应的行号
             # 注意：添加新行前，rowCount 就是新行的索引
             self._page_url_map[page_key] = self.resource_table.rowCount()
              
        self._seen_keys.add(dedup_key)
        
        # 保存资源对象
        self.resources.append((resource, engine_name))
        
        # 添加新行
        row = self.resource_table.rowCount()
        self.resource_table.insertRow(row)
        
        # 解析 URL 获取更多信息
        from urllib.parse import urlparse, unquote, parse_qs
        import re
        
        parsed_url = urlparse(resource.url)
        query_params = parse_qs(parsed_url.query)
        
        # 提取资源类型
        resource_type = self._get_resource_type(resource.url)
        
        # 从标题中提取文件名
        filename = resource.title
        
        # 提取清晰度/信息
        if getattr(resource, "is_variant", False) and getattr(resource, "quality_label", ""):
            quality_info = resource.quality_label
        else:
            quality_info = self._extract_quality_info(resource.url, query_params)
        
        # 提取来源域名
        source_domain = self._extract_source_domain(resource.url, resource.page_url)
        
        # 0 - 文件名
        title_item = QTableWidgetItem(f"📹 {filename}")
        title_item.setToolTip(f"文件名: {filename}\n完整URL: {resource.url}")
        self.resource_table.setItem(row, 0, title_item)
        
        # 1 - 类型（带颜色）
        type_item = QTableWidgetItem(TR(f"type_{resource_type.lower().replace(' ', '_')}"))
        type_item.setData(Qt.ItemDataRole.UserRole, resource_type) # 存储原始类型用于翻译
        type_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        if resource_type == 'M3U8':
            type_item.setForeground(Qt.GlobalColor.darkGreen)
        elif resource_type == 'MPD':
            type_item.setForeground(Qt.GlobalColor.darkBlue)
        elif resource_type in ['MP4', 'FLV', 'MKV', 'WEBM']:
            type_item.setForeground(Qt.GlobalColor.darkMagenta)
        self.resource_table.setItem(row, 1, type_item)
        
        # 2 - 清晰度
        quality_item = QTableWidgetItem(quality_info)
        quality_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.resource_table.setItem(row, 2, quality_item)
        
        # 3 - 来源网址
        domain_item = QTableWidgetItem(source_domain)
        domain_item.setToolTip(f"来源页面: {resource.page_url}")
        self.resource_table.setItem(row, 3, domain_item)
        
        # 4 - 引擎
        engine_icon = self._get_engine_icon(engine_name)
        engine_item = QTableWidgetItem(f"{engine_icon} {engine_name}")
        self.resource_table.setItem(row, 4, engine_item)
        
        # 5 - 检测时间
        time_item = QTableWidgetItem(resource.timestamp.strftime('%H:%M:%S'))
        time_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.resource_table.setItem(row, 5, time_item)
        
        # 6 - 下载按钮
        download_btn = QPushButton(TR("btn_download"))
        download_btn.setObjectName("success_button")
        download_btn.setProperty("resource_row", row)
        download_btn.clicked.connect(self._on_download_button_widget_clicked)
        
        self.resource_table.setCellWidget(row, 6, download_btn)
        
        logger.debug(f"资源已添加到表格: {resource.title}")

        # 更新来源过滤列表
        if self._features.get("ui_filter_search", True):
            self._update_source_filter(source_domain)
        
        # 如果是 M3U8 资源，后台解析 master playlist 获取可用分辨率
        if resource_type == 'M3U8' and not getattr(resource, "is_variant", False):
            self._parse_m3u8_variants(resource, row)

        # 应用过滤条件
        if self._features.get("ui_filter_search", True):
            self._apply_filters()
    
    def _on_download_button_widget_clicked(self):
        """通过按钮所在单元格反查当前行，避免闭包固化行号导致错位。"""
        button = self.sender()
        if button is None:
            logger.error("[UI] 下载按钮 sender 丢失")
            return

        current_row = -1
        for row in range(self.resource_table.rowCount()):
            if self.resource_table.cellWidget(row, 6) is button:
                current_row = row
                break

        if current_row < 0:
            fallback_row = button.property("resource_row")
            logger.warning(
                "[UI] 未能从表格反查按钮所在行，回退到初始行号",
                fallback_row=fallback_row,
            )
            if isinstance(fallback_row, int):
                current_row = fallback_row

        self._on_download_btn_clicked(current_row)

    def _extract_quality_info(self, url: str, query_params: dict) -> str:
        """从 URL 中提取清晰度/质量信息 - 返回简洁格式如 1080, 720(60)"""
        import re
        url_lower = url.lower()
        
        # YouTube: 从 itag 参数解析清晰度
        if 'googlevideo.com' in url or 'youtube.com' in url:
            itag = query_params.get('itag', [''])[0]
            if itag:
                # YouTube itag 对应清晰度表 - 简化格式
                itag_map = {
                    '18': '360', '22': '720', '37': '1080', '38': '2160',
                    '133': '240', '134': '360', '135': '480', '136': '720', 
                    '137': '1080', '138': '2160', '160': '144',
                    '298': '720(60)', '299': '1080(60)', 
                    '264': '1440', '266': '2160',
                    '140': '音频128k', '141': '音频256k', '251': '音频OPUS',
                    # 更多常见 itag
                    '243': '360', '244': '480', '247': '720', '248': '1080',
                    '271': '1440', '313': '2160', '302': '720(60)', '303': '1080(60)',
                }
                quality = itag_map.get(itag, f'itag{itag}')
                return quality
            else:
                # YouTube 但没有 itag，可能是主播放器请求，不显示清晰度
                return ""
        
        # 通用: 从 URL 路径中提取分辨率 + 帧率
        # 匹配 1080p60, 720p, etc
        match_with_fps = re.search(r'(\d{3,4})[pP](\d{2})', url)
        if match_with_fps:
            return f"{match_with_fps.group(1)}({match_with_fps.group(2)})"
        
        resolution_match = re.search(r'(\d{3,4})[pP]', url)
        if resolution_match:
            return resolution_match.group(1)
        
        # 从 URL 中提取数字分辨率标识 (更严格的匹配)
        if '/2160/' in url or '_2160.' in url or 'quality=2160' in url_lower:
            return "2160"
        if '/1440/' in url or '_1440.' in url or 'quality=1440' in url_lower:
            return "1440"
        if '/1080/' in url or '_1080.' in url or 'quality=1080' in url_lower:
            return "1080"
        if '/720/' in url or '_720.' in url or 'quality=720' in url_lower:
            return "720"
        if '/480/' in url or '_480.' in url or 'quality=480' in url_lower:
            return "480"
        if '/360/' in url or '_360.' in url or 'quality=360' in url_lower:
            return "360"
        
        # M3U8 特殊标识
        if '.m3u8' in url_lower:
            if 'master' in url_lower:
                return TR("type_master_playlist")
            if 'index' in url_lower or 'playlist' in url_lower:
                return TR("type_media_playlist")
        
        return ""
    
    def _find_resource_row(self, resource: M3U8Resource, fallback_row: int = -1) -> int:
        """Find the current row for a resource object after table mutations."""
        if 0 <= fallback_row < len(self.resources):
            current_resource, _engine_name = self.resources[fallback_row]
            if current_resource is resource:
                return fallback_row

        for row, (current_resource, _engine_name) in enumerate(self.resources):
            if current_resource is resource:
                return row
        return -1

    def _parse_m3u8_variants(self, resource: M3U8Resource, row: int):
        """后台解析 M3U8 master playlist，更新清晰度列"""
        thread = M3U8FetchThread(resource.url, resource.headers)
        
        def on_parsed(variants):
            current_row = self._find_resource_row(resource, row)
            if variants and 0 <= current_row < self.resource_table.rowCount():
                # 缓存到 resource 对象供下载时复用
                resource.variants = variants
                
                # 构建分辨率摘要文本
                heights = [v.get('height', 0) for v in variants if v.get('height', 0) > 0]
                if heights:
                    quality_text = '/'.join(f"{h}p" for h in sorted(set(heights), reverse=True))
                else:
                    quality_text = f"{len(variants)} variants"
                
                # 更新表格清晰度列
                quality_item = self.resource_table.item(current_row, 2)
                if quality_item:
                    quality_item.setText(quality_text)
                    quality_item.setToolTip(f"可用分辨率: {quality_text}")
                
                logger.info(f"M3U8 分辨率解析完成: {quality_text} ({resource.title})")

                # 为每个变体创建独立资源项（只生成一次）
                if not getattr(resource, "variants_listed", False):
                    resource.variants_listed = True
                    engine_name = None
                    if current_row < len(self.resources):
                        engine_name = self.resources[current_row][1]
                    engine_name = engine_name or TR("strategy_auto")

                    for variant in variants:
                        height = variant.get('height', 0)
                        resolution = variant.get('resolution') or ''
                        if height:
                            quality_label = f"{height}p"
                        elif resolution:
                            quality_label = resolution
                        else:
                            quality_label = "auto"

                        variant_title = resource.title
                        if quality_label and quality_label not in variant_title:
                            variant_title = f"{variant_title} [{quality_label}]"

                        variant_resource = M3U8Resource(
                            url=variant.get('url', resource.url),
                            headers=resource.headers,
                            page_url=resource.page_url,
                            title=variant_title,
                            page_title=resource.page_title,
                            selected_engine=getattr(resource, "selected_engine", None),
                        )
                        variant_resource.is_variant = True
                        variant_resource.variant_info = variant
                        variant_resource.quality_label = quality_label

                        self.add_resource(variant_resource, engine_name)
        
        thread.finished.connect(on_parsed)
        thread.start()
        self._m3u8_parse_threads.append(thread)  # 防止 GC
    
    def _get_resource_type(self, url: str) -> str:
        """从 URL 提取资源类型"""
        url_lower = url.lower()
        
        # 流媒体类型
        if '.m3u8' in url_lower or 'mpegurl' in url_lower:
            return 'M3U8'
        if '.mpd' in url_lower or 'dash' in url_lower:
            return 'MPD'
        
        # 视频文件类型
        type_map = {
            '.mp4': 'MP4',
            '.flv': 'FLV',
            '.mkv': 'MKV',
            '.webm': 'WEBM',
            '.avi': 'AVI',
            '.mov': 'MOV',
            '.wmv': 'WMV',
            '.m4v': 'M4V',
            '.ts': 'TS',
            '.3gp': '3GP',
        }
        
        for ext, type_name in type_map.items():
            if ext in url_lower.split('?')[0]:
                return type_name
        
        # 检查 URL 关键词
        if 'video' in url_lower or 'stream' in url_lower:
            return 'Video Stream'
        if 'playlist' in url_lower:
            return 'Playlist'
        
        return 'Unknown'
    
    def _extract_source_domain(self, url: str, page_url: str) -> str:
        """从 URL 或 page_url 提取来源网址"""
        # 优先使用 page_url（来源页面），否则回退到资源 URL
        target_url = page_url if page_url else url
        if not target_url:
            return TR('type_unknown')
        return target_url
    
    def _generate_dedup_key(self, resource: M3U8Resource) -> str:
        """
        生成智能去重键
        对于同一视频的不同CDN节点/清晰度，生成相同的键
        """
        from urllib.parse import urlparse, parse_qs
        import re
        
        url = resource.url
        title = resource.title
        parsed = urlparse(url)
        path = parsed.path.lower()
        url_lower = (url or "").lower()
        
        # M3U8 变体：确保 master 的不同清晰度可分别显示
        if getattr(resource, "is_variant", False) and getattr(resource, "variant_info", None):
            vinfo = resource.variant_info
            height = vinfo.get('height', 0)
            bandwidth = vinfo.get('tbr', 0)
            vurl = vinfo.get('url', '')
            return f"m3u8:variant:{height}:{bandwidth}:{vurl}"
        
        # YouTube 特殊处理：提取视频ID
        if 'googlevideo.com' in url or 'youtube.com' in url:
            # 尝试从 URL 参数中提取视频标识
            query = parse_qs(parsed.query)
            video_id = query.get('id', query.get('v', ['']))[0]
            itag = query.get('itag', [''])[0]  # itag 表示不同清晰度
            if video_id:
                # 同一视频ID + 同一清晰度 = 相同
                return f"youtube:{video_id}:{itag}"

        # M3U8：区分 master 与 media playlist
        if '.m3u8' in url_lower:
            if 'master' in url_lower:
                return f"m3u8:master:{path}"
            return f"m3u8:media:{path}"
        
        # 一般 M3U8/视频：用标题 + 文件名去重
        if title and title != "Unknown" and title != "未命名视频":
            # 提取文件名部分（去除 CDN 域名差异）
            filename = path.split('/')[-1].split('?')[0] if '/' in path else path
            return f"title:{title}:{filename}"
        
        # 默认：使用完整路径（忽略域名，因为 CDN 节点不同）
        return f"path:{path}"
    
    def _on_clear_clicked(self):
        """清空列表"""
        self.resource_table.setRowCount(0)
        self.resources.clear()
        self._seen_keys.clear()  # 同时清空去重集合
        self._page_url_map.clear()
        if self._features.get("ui_filter_search", True):
            self._reset_filters()
        logger.info("资源列表已清空")

    def _on_batch_download_clicked(self):
        """批量下载选中资源"""
        selected_rows = sorted({idx.row() for idx in self.resource_table.selectionModel().selectedRows()})
        if not selected_rows:
            return
        for row in selected_rows:
            self._on_download_btn_clicked(row)
        logger.info(f"批量下载: {len(selected_rows)} 条")

    def _on_batch_remove_clicked(self):
        """批量移除选中资源"""
        selected_rows = sorted({idx.row() for idx in self.resource_table.selectionModel().selectedRows()}, reverse=True)
        if not selected_rows:
            return

        for row in selected_rows:
            if 0 <= row < len(self.resources):
                self.resources.pop(row)
            self.resource_table.removeRow(row)

        self._rebuild_dedup_cache()
        if self._features.get("ui_filter_search", True):
            self._apply_filters()
        logger.info(f"批量移除: {len(selected_rows)} 条")

    def _reset_filters(self):
        """重置过滤条件"""
        if hasattr(self, "search_input"):
            self.search_input.clear()
        if hasattr(self, "type_filter"):
            self.type_filter.setCurrentIndex(0)
        if hasattr(self, "source_filter"):
            self.source_filter.clear()
            self.source_filter.addItems([TR("filter_all_sources")])
        if hasattr(self, "quality_filter"):
            self.quality_filter.setCurrentIndex(0)

    def _update_source_filter(self, source_domain: str):
        """动态更新来源过滤列表"""
        if not source_domain:
            return
        current = {self.source_filter.itemText(i) for i in range(self.source_filter.count())}
        if source_domain not in current:
            self.source_filter.addItem(source_domain)

    def _apply_filters(self):
        """应用搜索与过滤条件"""
        if not hasattr(self, "search_input"):
            return

        search_text = self.search_input.text().strip().lower()
        # 使用 index == 0 判断“全部”，避免依赖中英文文字比对
        type_is_all = not hasattr(self, "type_filter") or self.type_filter.currentIndex() == 0
        source_is_all = not hasattr(self, "source_filter") or self.source_filter.currentIndex() == 0
        quality_is_all = not hasattr(self, "quality_filter") or self.quality_filter.currentIndex() == 0

        type_filter = self.type_filter.currentText() if not type_is_all else ""
        source_filter = self.source_filter.currentText() if not source_is_all else ""
        quality_filter = self.quality_filter.currentText().lower() if not quality_is_all else ""

        for row in range(self.resource_table.rowCount()):
            title_item = self.resource_table.item(row, 0)
            type_item = self.resource_table.item(row, 1)
            quality_item = self.resource_table.item(row, 2)
            source_item = self.resource_table.item(row, 3)

            title_text = title_item.text().lower() if title_item else ""
            title_tip = title_item.toolTip().lower() if title_item else ""
            type_text = type_item.text() if type_item else ""
            quality_text = quality_item.text().lower() if quality_item else ""
            source_text = source_item.text() if source_item else ""

            visible = True

            if search_text:
                if search_text not in title_text and search_text not in title_tip and search_text not in source_text.lower():
                    visible = False

            if visible and not type_is_all and type_text != type_filter:
                visible = False

            if visible and not source_is_all and source_text != source_filter:
                visible = False

            if visible and not quality_is_all:
                if quality_filter not in quality_text:
                    visible = False

            self.resource_table.setRowHidden(row, not visible)

    def _rebuild_dedup_cache(self):
        """重建去重与 page_url 映射缓存"""
        self._seen_keys.clear()
        self._page_url_map.clear()

        # 复用 add_resource 中的标题去重逻辑
        platform_patterns = {
            ('youtube.com', 'youtu.be', 'googlevideo.com'): (' - YouTube', 'youtube'),
            ('bilibili.com', 'bilivideo.com', 'b23.tv'): ('_哔哩哔哩_bilibili', 'bilibili'),
            ('tiktok.com', 'tiktokcdn.com'): (' | TikTok', 'tiktok'),
            ('douyin.com', 'douyincdn.com'): ('', 'douyin'),
            ('twitter.com', 'x.com', 'twimg.com'): ('', 'twitter'),
            ('instagram.com', 'cdninstagram.com'): ('', 'instagram'),
            ('vimeo.com', 'vimeocdn.com'): (' on Vimeo', 'vimeo'),
            ('twitch.tv', 'twitchcdn.net'): (' - Twitch', 'twitch'),
            ('ixigua.com',): ('', 'ixigua'),
            ('youku.com', 'ykimg.com'): ('', 'youku'),
            ('iqiyi.com', 'iqiyipic.com'): ('', 'iqiyi'),
            ('v.qq.com', 'gtimg.cn'): ('', 'qqvideo'),
            ('facebook.com', 'fbcdn.net'): ('', 'facebook'),
            ('dailymotion.com',): (' - Dailymotion', 'dailymotion'),
        }

        for row, (resource, _) in enumerate(self.resources):
            dedup_key = self._generate_dedup_key(resource)
            self._seen_keys.add(dedup_key)

            url_str = str(resource.url).lower()
            page_url_str = str(resource.page_url).lower() if resource.page_url else ""
            combined_url = url_str + page_url_str

            matched_platform = None
            title_suffix = ''
            for domains, (suffix, platform_name) in platform_patterns.items():
                if any(domain in combined_url for domain in domains):
                    matched_platform = platform_name
                    title_suffix = suffix
                    break

            if matched_platform and resource.title:
                clean_title = resource.title
                if title_suffix:
                    clean_title = clean_title.replace(title_suffix, "")
                clean_title = clean_title.strip()
                if clean_title:
                    title_key = f"{matched_platform}_title:{clean_title}"
                    self._seen_keys.add(title_key)

            if hasattr(resource, 'page_url') and resource.page_url and \
               ('youtube.com' in str(resource.page_url) or 'youtu.be' in str(resource.page_url)):
                page_key = f"page_url_dedup:{resource.page_url}"
                self._page_url_map[page_key] = row
    
    def _get_engine_icon(self, name: str) -> str:
        """获取引擎图标"""
        icons = {
            'N_m3u8DL-RE': '⚡',
            'yt-dlp': '📺',
            'Streamlink': '🎥',
            'Aria2': '🚀'
        }
        return icons.get(name, '🤖')
    
    def clear(self):
        """清空列表"""
        self.resource_table.setRowCount(0)
        self.resources.clear()
        self._seen_keys.clear()
        self._page_url_map.clear()
