import os
import time
from pathlib import Path

import tomli_w

from quant.config import Settings


def save_settings(settings: Settings, path="config.toml"):
    path = Path(path)
    checked = Settings(**settings.model_dump(mode="json"))
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(tomli_w.dumps(checked.model_dump(mode="json")), encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(temporary, path)
            break
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))
