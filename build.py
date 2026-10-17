from pathlib import Path
import subprocess,sys,shutil
p=Path(__file__).resolve().parent
args=['--noconfirm','--clean','--onefile','--windowed','--name','CommentAssistant','--icon',str(p/'assets/icon.ico'),'--paths',str(p/'app'),'--distpath',str(p/'dist'),'--workpath',str(p/'build'),'--specpath',str(p),'--hidden-import','pystray._win32','--collect-all','playwright','--collect-submodules','uvicorn','--add-data',str(p/'app')+';app',str(p/'launcher.py')]
subprocess.run([sys.executable,'-m','PyInstaller',*args],check=True)
