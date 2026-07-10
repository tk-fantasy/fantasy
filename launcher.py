"""Aether Demo Launcher - 自动定位项目目录并启动，首次运行自动创建桌面快捷方式"""
import os
import sys
import subprocess
import ctypes
from pathlib import Path

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False

def get_desktop_path():
    """获取桌面路径"""
    return Path(os.environ['USERPROFILE']) / 'Desktop'

def create_shortcut(project_dir):
    """在桌面创建快捷方式"""
    desktop = get_desktop_path()
    shortcut_path = desktop / 'Aether Demo.lnk'

    # 如果快捷方式已存在，不覆盖
    if shortcut_path.exists():
        return

    exe_path = Path(project_dir) / 'Aether.exe'
    if not exe_path.exists():
        return

    try:
        import win32com.client
        shell = win32com.client.Dispatch('WScript.Shell')
        shortcut = shell.CreateShortcut(str(shortcut_path))
        shortcut.TargetPath = str(exe_path)
        shortcut.WorkingDirectory = str(project_dir)
        shortcut.IconLocation = str(exe_path) + ',0'
        shortcut.Description = 'Aether Demo - Smart Home Control'
        shortcut.Save()
    except ImportError:
        # 如果没有 win32com，用 PowerShell 创建
        ps_script = f"""
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut('{shortcut_path}')
        $shortcut.TargetPath = '{exe_path}'
        $shortcut.WorkingDirectory = '{project_dir}'
        $shortcut.IconLocation = '{exe_path},0'
        $shortcut.Description = 'Aether Demo - Smart Home Control'
        $shortcut.Save()
        """
        subprocess.run(['powershell', '-NoProfile', '-Command', ps_script],
                      capture_output=True, timeout=10)

def main():
    # 获取 exe 所在目录（即项目根目录）
    if getattr(sys, 'frozen', False):
        project_dir = os.path.dirname(sys.executable)
    else:
        project_dir = os.path.dirname(os.path.abspath(__file__))

    # 首次运行自动创建桌面快捷方式
    create_shortcut(project_dir)

    bat_path = os.path.join(project_dir, 'run_demo_fixed.bat')

    if not os.path.exists(bat_path):
        input(f"Error: Cannot find run_demo_fixed.bat in {project_dir}\nPress Enter to exit...")
        sys.exit(1)

    # 启动 bat
    subprocess.Popen(
        bat_path,
        cwd=project_dir,
        shell=True
    )

if __name__ == '__main__':
    main()
