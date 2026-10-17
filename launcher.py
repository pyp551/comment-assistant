"""Desktop entry. Uses installed Edge app window; no sign-in or posting on launch."""
import os,sys,socket,subprocess,time,threading,webbrowser,ctypes
from pathlib import Path
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'app'))
if os.name=='nt':ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('CommentWorkbench.Desktop.1')
from storage import relocate
try:relocate()
except Exception as exc:
 if os.name=='nt':ctypes.windll.user32.MessageBoxW(None,'目录迁移未完成，继续使用原数据。\n'+str(exc),'评论区小助手',0)
import server
import requests

def main():
 url='http://127.0.0.1:'+str(server.PORT)
 def open_window():
  time.sleep(1.5)
  if server.EDGE.exists():
   subprocess.Popen([str(server.EDGE),'--app='+url,'--user-data-dir='+str(server.DATA/'ui-profile'),'--no-first-run','--no-default-browser-check'],creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
  else:webbrowser.open(url)
 try:
  sock=socket.socket();sock.bind(('127.0.0.1',server.PORT));sock.close()
 except OSError:
  try:
   session=requests.Session();session.trust_env=False
   identity=session.get(url+'/health',timeout=2).json()
   if identity.get('application')=='comment-assistant':open_window();return
  except Exception:pass
  if os.name=='nt':ctypes.windll.user32.MessageBoxW(None,'端口已被其他服务占用，请设置 COMMENT_WORKBENCH_PORT 更换端口。','评论区小助手',0)
  return
 import pystray
 from PIL import Image
 exiting=threading.Event()
 def exit_from_tray(icon,item):
  if exiting.is_set():return
  exiting.set();icon.title='评论区小助手：正在等待发送收尾';icon.update_menu()
  def request_exit():
   try:
    session=requests.Session();session.trust_env=False
    response=session.post(url+'/api/exit-app',headers={'x-comment-token':server.TOKEN,'Origin':url},timeout=10)
    response.raise_for_status()
   except Exception:
    exiting.clear();icon.title='评论区小助手';icon.update_menu()
    ctypes.windll.user32.MessageBoxW(None,'退出请求未成功，请打开界面确认状态后重试。未强制结束程序。','评论区小助手',0)
  threading.Thread(target=request_exit,daemon=True).start()
 icon=pystray.Icon('CommentAssistant',Image.open(ROOT/'app/web/icon.png'),'评论区小助手',menu=pystray.Menu(
  pystray.MenuItem('打开评论区小助手',lambda icon,item:threading.Thread(target=open_window,daemon=True).start(),default=True),
  pystray.MenuItem('退出软件（等待发送收尾）',exit_from_tray,enabled=lambda item:not exiting.is_set())
 ))
 server.desktop_exit_hook=icon.stop
 def ready(icon):
  icon.visible=True
  threading.Thread(target=open_window,daemon=True).start()
 icon.run_detached(setup=ready)
 try:server.uvicorn.run(server.app,host='127.0.0.1',port=server.PORT,log_level='warning',log_config=None,access_log=False,loop='asyncio')
 finally:icon.stop()
if __name__=='__main__':main()
