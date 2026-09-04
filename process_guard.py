"""Find old polling processes for this exact bot token. Never print credentials."""
import argparse
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from dotenv import dotenv_values
from config import BOT_TOKEN

def inspect_process(pid, token, proc_root=Path('/proc')):
    if pid == os.getpid():
        return None
    root=proc_root/str(pid)
    try:
        args=root.joinpath('cmdline').read_bytes().decode(errors='replace').split('\0')
        if not args or not Path(args[0]).name.startswith('python'):
            return None
        scripts=[arg for arg in args[1:] if arg.endswith('.py')]
        if not scripts:
            return None
        cwd=root.joinpath('cwd').resolve()
        script=Path(scripts[0])
        if not script.is_absolute():
            script=cwd/script
        source=script.read_text(encoding='utf-8')
        if 'start_polling(' not in source or 'aiogram' not in source:
            return None
        environ=dict(item.split('=',1) for item in root.joinpath('environ').read_text().split('\0') if '=' in item)
        env_file=Path(environ.get('ENV_FILE',str(cwd/'.env')))
        if not env_file.is_absolute():
            env_file=cwd/env_file
        settings=dotenv_values(env_file) if env_file.is_file() else {}
        actual=environ.get('BOT_TOKEN') or settings.get('BOT_TOKEN')
        if not token or actual != token:
            return None
        # /proc stat comm may contain spaces; fields after ')' start at field 3.
        start_time=root.joinpath('stat').read_text().rsplit(')',1)[1].split()[19]
        cgroup=root.joinpath('cgroup').read_text()
        units=re.findall(r'/([A-Za-z0-9_.@-]+\.service)(?:/|\n|$)',cgroup)
        return {'pid':pid,'cwd':str(cwd),'script':str(script),'start':start_time,
                'units':[u for u in units if not u.startswith('user@')]}
    except (OSError,ValueError,IndexError):
        return None

def discover(token,proc_root=Path('/proc')):
    if not proc_root.is_dir():
        return []
    found=[]
    for entry in proc_root.iterdir():
        if entry.name.isdecimal():
            process=inspect_process(int(entry.name),token,proc_root)
            if process:
                found.append(process)
    return found

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--stop-old',action='store_true')
    args=parser.parse_args()
    matches=discover(BOT_TOKEN)
    if not matches:
        print('Других доступных для проверки Python-копий с этим токеном на сервере не найдено.')
        print('Копию на другом сервере или компьютере эта проверка увидеть не может.')
        return
    for process in matches:
        pid=process['pid']
        print(f"Найдена старая копия: PID {pid}, папка {process['cwd']}")
        if not args.stop_old:
            continue
        current=inspect_process(pid,BOT_TOKEN)
        if not current or current['start']!=process['start']:
            continue
        stopped=False
        for unit in current['units']:
            for scope in (['systemctl','--user'],['sudo','systemctl']):
                info=subprocess.run(scope+['show',unit,'-p','MainPID','--value'],capture_output=True,text=True)
                # Do not stop parent services such as SSH or VS Code.
                if info.returncode==0 and info.stdout.strip()==str(pid):
                    subprocess.run(scope+['disable','--now',unit],check=True)
                    stopped=True
                    break
            if stopped:
                break
        if not stopped:
            os.kill(pid,signal.SIGTERM)
        deadline=time.monotonic()+5
        while time.monotonic()<deadline and inspect_process(pid,BOT_TOKEN):
            time.sleep(0.1)
        if inspect_process(pid,BOT_TOKEN):
            raise SystemExit(f'Копия PID {pid} не завершилась. Остановите её вручную и повторите обновление.')
        print('Старая копия остановлена. Её база и исходники сохранены.')
    if args.stop_old and discover(BOT_TOKEN):
        raise SystemExit('Старая копия появилась снова. Нужно отключить её автоматический запуск.')

if __name__=='__main__':
    main()

