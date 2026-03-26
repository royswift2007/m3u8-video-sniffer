import sys
import os
import shutil
from pathlib import Path

# Ensure we can import from the parent directory
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

try:
    from utils.config_manager import config
except ImportError:
    print("Error: Could not import utils.config_manager.")
    sys.exit(1)

def clear_cache():
    print("=" * 40)
    print("M3U8 Video Sniffer - Cache Cleaner")
    print("=" * 40)
    
    try:
        temp_dir = config.get("temp_dir")
        if not temp_dir:
            print("[!] Temp directory not configured in config.json")
            return

        temp_path = Path(temp_dir)
        print(f"[*] Target Directory: {temp_path}")
        
        if not temp_path.exists():
            print("[-] Directory does not exist. Nothing to clean.")
            return

        print("[*] Cleaning...")
        
        # Count files before
        files_removed = 0
        bytes_removed = 0
        
        # Iterate and remove contents
        for item in temp_path.iterdir():
            try:
                if item.is_dir():
                    # Calculate size for reporting (optional, but nice)
                     for r, d, f in os.walk(item):
                        for file in f:
                            try:
                                bytes_removed += os.path.getsize(os.path.join(r, file))
                            except: pass
                     shutil.rmtree(item)
                else:
                    bytes_removed += item.stat().st_size
                    item.unlink()
                files_removed += 1
            except Exception as e:
                print(f"[!] Failed to delete {item.name}: {e}")
        
        print(f"[+] Done! Cleaned {files_removed} items.")
        
        # Convert bytes to readable format
        if bytes_removed > 1024 * 1024 * 1024:
            size_str = f"{bytes_removed / (1024 * 1024 * 1024):.2f} GB"
        elif bytes_removed > 1024 * 1024:
            size_str = f"{bytes_removed / (1024 * 1024):.2f} MB"
        else:
            size_str = f"{bytes_removed / 1024:.2f} KB"
            
        print(f"[+] Freed approximately {size_str}")

    except Exception as e:
        print(f"[!] Error during cleanup: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    clear_cache()
    print("\nPress any key to exit...")
    os.system("pause >nul")
