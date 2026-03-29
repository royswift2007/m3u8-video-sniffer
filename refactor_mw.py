import os

def refactor_main_window():
    filepath = r"c:\Users\qinghua\Documents\M3U8D\ui\main_window.py"
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()

    # Find where _show_manual_dialog starts
    start_idx = -1
    for i, line in enumerate(lines):
        if "def _show_manual_dialog(self):" in line:
            start_idx = i
            break
    
    # Find where _handle_manual_browser_link starts
    end_idx = -1
    for i, line in enumerate(lines):
        if "def _handle_manual_browser_link(self, url):" in line:
            end_idx = i
            break

    if start_idx == -1 or end_idx == -1:
        print(f"Indices not found: {start_idx}, {end_idx}")
        return

    new_methods = """    def _show_manual_dialog(self):
        \"\"\"显示使用手册 (从外部 Markdown 加载)\"\"\"
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QTextBrowser
        from pathlib import Path
        import html
        
        dialog = QDialog(self)
        lang = i18n.get_language()
        dialog.setWindowTitle(TR("quick_manual"))
        dialog.setMinimumSize(900, 700)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(12, 12, 12, 12)

        # 快捷脚本列表 (按当前语言翻译说明)
        is_en = (lang == "en")
        quick_scripts = [
            (
                "download_tools.bat",
                "Download tools (N_m3u8DL-RE / yt-dlp) into bin/" if is_en else "下载 N_m3u8DL-RE / yt-dlp 等工具到 bin 目录",
            ),
            (
                "install_extensions.bat",
                "Open Chrome for extension installation" if is_en else "启动固定用户目录的 Chrome，用于安装扩展",
            ),
            (
                "register_protocol.bat",
                "Register m3u8dl:// protocol" if is_en else "注册 m3u8dl:// 协议（猫爪一键回传）",
            ),
            (
                "uninstall_protocol.bat",
                "Unregister protocol" if is_en else "卸载 m3u8dl:// 协议注册",
            ),
            (
                "clean_cache.bat",
                "Clean cache files" if is_en else "执行缓存清理脚本",
            ),
        ]

        quick_links_html = []
        for script_name, script_desc in quick_scripts:
            quick_links_html.append(
                f\"\"\"
                <div style="margin-bottom: 12px;">
                    <a href="script:///{script_name}" style="text-decoration:none; font-weight:700; color:#1f4e79;">{html.escape(script_name)}</a><br>
                    <span style="color:#66717d;">{"Description" if is_en else "说明"}：{html.escape(script_desc)}</span>
                </div>
                \"\"\"
            )

        # 加载外部文本
        manual_path = Path(__file__).parent.parent / "resources" / f"manual_{lang}.md"
        manual_text = ""
        if manual_path.exists():
            try:
                with open(manual_path, "r", encoding="utf-8") as f:
                    manual_text = f.read()
            except Exception as e:
                manual_text = f"Error loading manual: {e}"
        else:
            manual_text = f"Manual file not found: {manual_path}"

        manual_html = f\"\"\"
        <html>
          <body style="font-family:'Microsoft YaHei','Segoe UI',sans-serif; color:#243447;">
            <div style="background:#faf7f1; border:1px solid #e4ddd0; border-radius:8px; padding:12px 14px; margin-bottom:14px;">
              <div style="font-weight:700; font-size:16px; color:#17324d; margin-bottom:6px;">{TR("quick_manual")} - Scripts</div>
              {''.join(quick_links_html)}
            </div>
            <pre style="white-space:pre-wrap; font-family:'Consolas','Microsoft YaHei UI','Segoe UI',sans-serif; font-size:13px; line-height:1.55; margin:0;">{html.escape(manual_text)}</pre>
          </body>
        </html>
        \"\"\"

        manual_view = QTextBrowser()
        manual_view.setReadOnly(True)
        manual_view.setOpenLinks(False)
        manual_view.setOpenExternalLinks(False)
        manual_view.setHtml(manual_html)
        manual_view.anchorClicked.connect(self._handle_manual_browser_link)
        layout.addWidget(manual_view, 1)

        dialog.setLayout(layout)
        dialog.exec()

"""
    # Replace the block
    new_content = lines[:start_idx] + [new_methods] + lines[end_idx:]
    
    with open(filepath, "w", encoding="utf-8") as f:
        f.writelines(new_content)
    print("Successfully refactored main_window.py")

if __name__ == "__main__":
    refactor_main_window()
