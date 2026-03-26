"""
M3U8 Video Sniffer - 全能视频下载工具
应用程序入口
"""
import sys
import argparse
import json
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QTimer

from core.app_paths import get_resource_path, initialize_runtime_directories
from core.dependency_checker import get_missing_required_dependencies
from core.dependency_installer import install_required_dependencies
from core.dependency_prompt import (
    show_dependency_install_failure,
    show_missing_dependency_confirmation,
)
from ui.main_window import MainWindow
from utils.logger import logger
from utils.config_manager import config


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description='M3U8 Video Sniffer')
    parser.add_argument('--url', type=str, help='视频 URL（来自猫爪等外部程序）')
    parser.add_argument('--headers', type=str, help='请求头 JSON 字符串')
    parser.add_argument('--filename', type=str, help='保存文件名')
    return parser.parse_args()


def main():
    """主函数"""
    # 解析命令行参数
    args = parse_args()
    
    # 关键：禁用 Chromium 自动化控制特征 (解决 Google 登录拦截)
    # 必须在 QApplication 实例化之前设置
    import os
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-blink-features=AutomationControlled"
    
    # 创建应用
    app = QApplication(sys.argv)
    app.setApplicationName("M3U8 Video Sniffer")
    app.setOrganizationName("M3U8VideoSniffer")
    
    # 设置全局图标
    from PyQt6.QtGui import QIcon
    icon_path = get_resource_path("icon.png")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    
    # 创建主窗口
    try:
        initialize_runtime_directories()
        logger.info("="*60)
        logger.info("M3U8 Video Sniffer 启动中...")
        logger.info("="*60)

        missing_required_dependencies = get_missing_required_dependencies()
        if missing_required_dependencies:
            logger.warning(
                "检测到缺失的必须依赖",
                missing_count=len(missing_required_dependencies),
                missing_items=", ".join(
                    entry.relative_path for entry in missing_required_dependencies
                ),
            )
            if not show_missing_dependency_confirmation(missing_required_dependencies):
                logger.info("用户取消缺失依赖确认，程序退出")
                sys.exit(0)

            logger.info(
                "用户确认缺失依赖提示，开始下载必须依赖",
                missing_count=len(missing_required_dependencies),
            )
            install_result = install_required_dependencies(missing_required_dependencies)
            if not install_result.ok:
                error_messages = install_result.get_error_messages()
                logger.critical(
                    "必须依赖下载失败，程序退出",
                    failed_count=install_result.failed_count,
                    errors=" | ".join(error_messages),
                )
                show_dependency_install_failure(error_messages)
                sys.exit(1)

            logger.info(
                "必须依赖下载完成，继续启动主界面",
                success_count=install_result.success_count,
                skipped_count=install_result.skipped_count,
            )

        window = MainWindow()
        window.show()
        
        # 如果有命令行传入的 URL，延迟添加到资源列表
        if args.url:
            logger.info(f"[CLI] 收到外部 URL: {args.url}")
            
            # 解析 headers
            headers = {}
            if args.headers:
                try:
                    headers = json.loads(args.headers)
                except:
                    logger.warning(f"[CLI] 无法解析 headers: {args.headers}")
            
            # 使用 QTimer 延迟执行，确保窗口完全初始化
            def add_external_resource():
                from core.task_model import M3U8Resource
                from core.engine_selector import EngineSelector
                
                resource = M3U8Resource(
                    url=args.url,
                    headers=headers,
                    page_url=args.url,
                    title=args.filename or "External Download"
                )
                
                selector = EngineSelector(window.engines)
                _, engine_name = selector.select(args.url)
                window.resource_panel.add_resource(resource, engine_name)
                
                # 切换到资源标签页
                window.main_tabs.setCurrentIndex(1)
                
                logger.info(f"[CLI] 已添加资源: {args.filename or args.url}")
            
            QTimer.singleShot(500, add_external_resource)
        
        # 运行事件循环
        sys.exit(app.exec())
        
    except Exception as e:
        logger.critical(f"应用启动失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
