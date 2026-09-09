

import os
import sys
import json
import zipfile
import shutil
import tempfile

GMOD_REQUIRED = ['name', 'uuid', 'version', 'min_support_version', 'description']
GMLIB_REQUIRED = ['name', 'python_version', 'platform', 'liblist']

LIBLIST_PATH = os.path.join(os.getcwd(), 'liblist.json')

def validate_manifest(manifest, required_fields):
    missing = [f for f in required_fields if f not in manifest]
    if missing:
        raise ValueError(f"manifest 缺少字段: {', '.join(missing)}")

def find_whl_for_package(search_dir, pkg_name):
    pkg_prefix = pkg_name.lower().replace('_', '-') + '-'
    for root, _, files in os.walk(search_dir):
        for f in files:
            if f.lower().endswith('.whl') and f.lower().startswith(pkg_prefix):
                return os.path.join(root, f)
    return None

def load_liblist():
    if os.path.exists(LIBLIST_PATH):
        try:
            with open(LIBLIST_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
                else:
                    print("警告: liblist.json 格式错误，重置为空列表")
                    return []
        except Exception:
            return []
    else:
        return []

def save_liblist(liblist):
    with open(LIBLIST_PATH, 'w', encoding='utf-8') as f:
        json.dump(liblist, f, indent=2)

def process_gmmod(zip_path):
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            if 'manifest.json' not in zf.namelist():
                print("错误: zip 中缺少 manifest.json")
                return False

            with zf.open('manifest.json') as f:
                manifest = json.load(f)

            validate_manifest(manifest, GMOD_REQUIRED)
            uuid = manifest.get('uuid')
            if not uuid:
                print("错误: manifest 缺少 uuid")
                return False

            if 'libs' in manifest and not isinstance(manifest['libs'], list):
                print("错误: libs 字段必须为列表（如果提供）")
                return False

            target_dir = os.path.join(os.getcwd(), uuid)
            if os.path.exists(target_dir):
                shutil.rmtree(target_dir)
            os.makedirs(target_dir, exist_ok=True)

            zf.extractall(target_dir)
            print(f"解压到 {target_dir}")

    except Exception as e:
        print(f"处理 gmmod 失败: {e}")
        return False

    print("插件解压成功（结构校验通过）")
    return True

def process_gmlib(zip_path):
    temp_dir = tempfile.mkdtemp(prefix='gmlib_')
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(temp_dir)

        manifest_path = os.path.join(temp_dir, 'manifest.json')
        if not os.path.isfile(manifest_path):
            print("错误: 解压后找不到 manifest.json")
            return False

        with open(manifest_path, 'r', encoding='utf-8') as f:
            manifest = json.load(f)

        validate_manifest(manifest, GMLIB_REQUIRED)
        liblist_pkgs = manifest.get('liblist', [])
        if not isinstance(liblist_pkgs, list):
            print("错误: liblist 字段必须为列表")
            return False

        global_liblist = load_liblist()
        lib_dir = os.path.join(os.getcwd(), 'lib')
        os.makedirs(lib_dir, exist_ok=True)

        success = True
        for pkg in liblist_pkgs:
            whl_path = find_whl_for_package(temp_dir, pkg)
            if whl_path is None:
                print(f"错误: 找不到包 '{pkg}' 对应的 .whl 文件")
                success = False
                continue

            whl_filename = os.path.basename(whl_path)
            if whl_filename in global_liblist:
                print(f"  - {whl_filename} 已存在，跳过")
                continue

            dest = os.path.join(lib_dir, whl_filename)
            shutil.copy2(whl_path, dest)
            print(f"  - 复制 {whl_filename} -> {dest}")
            global_liblist.append(whl_filename)

        save_liblist(global_liblist)
        return success

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

def load_plugin(plugin_path):
    if not plugin_path.lower().endswith(('.gmmod', '.gmlib')):
        print(f"错误: 不支持的文件类型，请使用 .gmmod 或 .gmlib")
        return False

    if not os.path.isfile(plugin_path):
        print(f"错误: 文件不存在 - {plugin_path}")
        return False

    if not zipfile.is_zipfile(plugin_path):
        print(f"错误: 文件不是有效的 ZIP 归档 - {plugin_path}")
        return False

    ext = os.path.splitext(plugin_path)[1].lower()
    if ext == '.gmmod':
        return process_gmmod(plugin_path)
    else:
        return process_gmlib(plugin_path)

if __name__ == '__main__':
    if len(sys.argv) != 2:
        print("用法: python loader.py <插件文件>")
        sys.exit(1)

    result = load_plugin(sys.argv[1])
    sys.exit(0 if result else 1)