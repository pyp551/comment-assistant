"""Explicit next-launch data relocation; never merges existing stores."""
import os,json,shutil,socket
from pathlib import Path
HOME=Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'CommentWorkbench'
ROUTE=HOME/'data-location.json'
def current():
 if os.environ.get('COMMENT_WORKBENCH_DATA'):return Path(os.environ['COMMENT_WORKBENCH_DATA']).resolve()
 return Path(json.loads(ROUTE.read_text('utf-8'))['path']).resolve() if ROUTE.exists() else HOME.resolve()
def schedule(raw):
 if os.environ.get('COMMENT_WORKBENCH_DATA'):raise ValueError('当前由环境变量指定目录，请修改启动环境变量后重启')
 target=Path(raw).expanduser()
 if not target.is_absolute() or target==Path(target.anchor):raise ValueError('请输入完整的专用文件夹路径，例如 D:/评论区小助手数据')
 target=target.resolve();src=current()
 if target==src or target in src.parents or src in target.parents:raise ValueError('请选择当前数据目录之外的新文件夹')
 if target.exists() and (not target.is_dir() or any(target.iterdir())):raise ValueError('目标必须是空文件夹，不能合并已有评论数据库')
 HOME.mkdir(parents=True,exist_ok=True)
 (HOME/'pending-location.json').write_text(json.dumps(dict(source=str(src),target=str(target)),ensure_ascii=False),'utf-8')
 return str(target)
def relocate():
 pending=HOME/'pending-location.json'
 if os.environ.get('COMMENT_WORKBENCH_DATA') or not pending.exists():return
 probe=socket.socket()
 try:probe.bind(('127.0.0.1',int(os.environ.get('COMMENT_WORKBENCH_PORT','8896'))))
 except OSError:return
 finally:probe.close()
 v=json.loads(pending.read_text('utf-8'));src=Path(v['source']);dst=Path(v['target'])
 if src.resolve()!=current():raise ValueError('迁移来源已变化，保留原目录，请重新选择')
 if dst.exists() and any(dst.iterdir()):raise ValueError('目标目录已有文件，未迁移；原数据保持不变')
 dst.mkdir(parents=True,exist_ok=True)
 # The app has exited before this runs. Browser login profiles are deliberately not copied.
 for f in src.iterdir():
  if f.name in ('ui-profile','edge-profile','pending-location.json','data-location.json'):continue
  if f.is_dir():shutil.copytree(f,dst/f.name)
  else:shutil.copy2(f,dst/f.name)
 temp=HOME/'data-location.tmp';temp.write_text(json.dumps(dict(path=str(dst)),ensure_ascii=False),'utf-8');os.replace(temp,ROUTE)
 pending.unlink()
