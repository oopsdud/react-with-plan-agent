"""Local model configuration; secrets stay in a Git-ignored server-side file."""
import json
import os
import tempfile
from pathlib import Path
from threading import RLock
from urllib.parse import urlsplit

CONFIG_PATH = Path(__file__).resolve().parents[2] / 'data' / 'model-config.json'
CONFIG_LOCK = RLock()
ALLOWED_FIELDS = {'LLM_BASE_URL', 'LLM_API_KEY', 'LLM_MODEL', 'LLM_JSON_MODE'}


def load_model_config():
    with CONFIG_LOCK:
        if not CONFIG_PATH.exists():
            return {}
        try:
            value = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
            if not isinstance(value, dict) or not set(value).issubset(ALLOWED_FIELDS):
                raise ValueError()
            if any(not isinstance(v, bool if k == 'LLM_JSON_MODE' else str) for k,v in value.items()):
                raise ValueError()
            return value
        except (ValueError, OSError):
            raise RuntimeError('模型配置文件无法读取，请检查 data/model-config.json。') from None


def save_model_config(value):
    with CONFIG_LOCK:
        CONFIG_PATH.parent.mkdir(parents=True,exist_ok=True)
        handle, name = tempfile.mkstemp(prefix='.model-',suffix='.tmp',dir=CONFIG_PATH.parent)
        try:
            with os.fdopen(handle,'w',encoding='utf-8') as stream:
                json.dump(value,stream,ensure_ascii=False,indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name,CONFIG_PATH)
        finally:
            if os.path.exists(name):os.unlink(name)


def normalize_base_url(value):
    value=value.strip().rstrip('/')
    url=urlsplit(value)
    if url.scheme not in ('http','https') or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('请输入有效的 HTTP/HTTPS API 基础地址，不要包含密钥、查询参数或 fragment。')
    return value
