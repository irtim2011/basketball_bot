import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone

def backup(source, directory):
    target_dir = Path(directory)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / ('attendance-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f') + '.db')
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    print('Резервная копия:', target)
    return target

if __name__ == '__main__':
    backup(sys.argv[1], sys.argv[2])

