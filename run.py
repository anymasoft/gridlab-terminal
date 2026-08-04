"""Точка входа GRIDLAB. Запускать это (PyCharm: правый клик → Run 'run').
Работает из любого cwd. Порт можно сменить через переменную окружения PORT."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import uvicorn  # noqa: E402

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8100"))
    print(f"GRIDLAB -> http://localhost:{port}")
    uvicorn.run("app.main:app", host="127.0.0.1", port=port, reload=False)
