import os
import re
import sys

def get_used_keys():
    keys = set()
    pattern = re.compile(r'TR\(\s*["\']([^"\']+)["\']\s*\)')
    search_dirs = ['ui', 'core', '.']
    
    for subdir in search_dirs:
        dir_path = os.path.join(os.getcwd(), subdir)
        if not os.path.isdir(dir_path): continue
        for root, _, files in os.walk(dir_path):
            for file in files:
                if file.endswith('.py') and file != 'i18n_data.py':
                    with open(os.path.join(root, file), 'r', encoding='utf-8') as f:
                        for line in f:
                            matches = pattern.findall(line)
                            for m in matches:
                                keys.add(m)
    return keys

def check_keys():
    sys.path.append(os.getcwd())
    from utils.i18n_data import TRANSLATIONS
    
    zh_keys = set(TRANSLATIONS.get('zh', {}).keys())
    en_keys = set(TRANSLATIONS.get('en', {}).keys())
    
    used_keys = get_used_keys()
    
    missing_in_zh = used_keys - zh_keys
    missing_in_en = used_keys - en_keys
    
    with open('missing_utf8.txt', 'w', encoding='utf-8') as f:
        f.write("Missing in zh:\n")
        for key in sorted(missing_in_zh):
            f.write(f"{key}\n")
            
        f.write("\nMissing in en:\n")
        for key in sorted(missing_in_en):
            f.write(f"{key}\n")

if __name__ == '__main__':
    check_keys()
