"""QiMind（棋思）全局配置。

集中管理引擎路径、DeepSeek 凭据、分析参数与缓存目录。
所有可调项都可以用环境变量或 ``qimind/data/settings.json`` 覆盖，
环境变量优先级最高：

======================  ==========================
环境变量                 作用
======================  ==========================
``QIMIND_ENGINE``        Pikafish 可执行文件路径
``QIMIND_DEEPSEEK_KEY``  DeepSeek API Key
``DEEPSEEK_API_KEY``     DeepSeek API Key（兼容写法）
``QIMIND_MODEL``         模型名（deepseek-flash / deepseek-v4-pro）
======================  ==========================
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = Path(__file__).resolve().parent
DATA_DIR = PACKAGE_DIR / "data"
CACHE_DIR = DATA_DIR / "cache"
SETTINGS_FILE = DATA_DIR / "settings.json"

#: cchess 库源码目录（未安装为第三方包时通过 sys.path 引入）
CCHESS_SRC = PROJECT_ROOT / "cchess" / "src"

#: 引擎搜索顺序，越靠前优先级越高
ENGINE_CANDIDATES: List[Path] = [
    PROJECT_ROOT / "cchess" / "Engine" / "pikafish_230408" / "pikafish.exe",
    PROJECT_ROOT / "cchess" / "Engine" / "pikafish_230408" / "pikafish-avx2.exe",
    PROJECT_ROOT / "cchess" / "Engine" / "pikafish_230408" / "pikafish-bmi2.exe",
    PROJECT_ROOT / "cchess" / "Engine" / "pikafish_32bit" / "pikafish.exe",
    PROJECT_ROOT / "Pikafish" / "pikafish.exe",
    PROJECT_ROOT / "Pikafish" / "src" / "pikafish.exe",
]

DEEPSEEK_BASE_URL = "https://api.deepseek.com"

#: 默认分析参数
DEFAULTS: Dict[str, Any] = {
    "engine_path": "",
    "depth": 16,
    "multipv": 4,
    "threads": 4,
    "hash_mb": 256,
    "model": "deepseek-flash",
    "reasoning_effort": "high",
    "explain_scope": "all",
}


def ensure_data_dir() -> Path:
    """确保数据目录存在并返回它。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


_RUNTIME_SETTINGS: Optional[Dict[str, Any]] = None


def load_settings(refresh: bool = False) -> Dict[str, Any]:
    """读取配置：默认值 < ``data/settings.json`` < 环境变量。"""
    global _RUNTIME_SETTINGS
    if _RUNTIME_SETTINGS is not None and not refresh:
        return dict(_RUNTIME_SETTINGS)

    settings = dict(DEFAULTS)
    settings.update(_read_json(SETTINGS_FILE))

    env_map = {
        "engine_path": "QIMIND_ENGINE",
        "model": "QIMIND_MODEL",
    }
    for key, env_name in env_map.items():
        value = os.environ.get(env_name)
        if value:
            settings[key] = value

    if os.environ.get("QIMIND_DEPTH"):
        try:
            settings["depth"] = int(os.environ["QIMIND_DEPTH"])
        except ValueError:
            pass

    _RUNTIME_SETTINGS = settings
    return dict(settings)


def save_settings(values: Dict[str, Any]) -> Dict[str, Any]:
    """把部分配置写回 ``data/settings.json``，返回更新后的完整配置。"""
    ensured = ensure_data_dir()
    store = _read_json(SETTINGS_FILE)
    store.update({k: v for k, v in values.items() if v is not None})
    path = ensured / "settings.json"
    path.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    return load_settings(refresh=True)


def deepseek_api_key() -> Optional[str]:
    """解析 DeepSeek API Key：界面上保存的优先，其次是环境变量。"""
    return saved_api_key() or env_api_key() or None


def env_api_key() -> Optional[str]:
    """从环境变量读取 DeepSeek API Key。"""
    for env_name in ("QIMIND_DEEPSEEK_KEY", "DEEPSEEK_API_KEY", "QIMIND_DEEPSEEK_API_KEY"):
        value = os.environ.get(env_name)
        if value:
            return value.strip()
    return None


def saved_api_key() -> Optional[str]:
    """读取界面上保存过的 DeepSeek API Key（``data/secrets.json``）。"""
    for file_name, field in (("secrets.json", "deepseek_api_key"),):
        data = _read_json(DATA_DIR / file_name)
        value = data.get(field) or data.get("api_key")
        if value:
            return str(value).strip()

    settings = load_settings()
    value = settings.get("deepseek_api_key")
    return str(value).strip() if value else None


def key_source() -> str:
    """返回当前 Key 的来源：``saved``（界面保存）/ ``env``（环境变量）/ ``""``。"""
    if saved_api_key():
        return "saved"
    if env_api_key():
        return "env"
    return ""


def mask_key(key: Optional[str]) -> str:
    """把 Key 打码后再展示，避免界面或日志泄露完整凭据。"""
    if not key:
        return ""
    key = key.strip()
    if len(key) <= 12:
        return "已配置（长度 %d）" % len(key)
    return f"{key[:6]}…{key[-4:]}"


#: 形如 ``sk-xxxx`` 的密钥片段（用于把报错信息里的 Key 抹掉）
_SECRET_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-]{6,}")


def scrub_secrets(text: str) -> str:
    """把文本里可能出现的 API Key 打码，避免报错信息泄露凭据。"""
    return _SECRET_PATTERN.sub("sk-***已隐藏***", text or "")


def save_deepseek_key(key: Optional[str]) -> None:
    """保存或清除 DeepSeek API Key（写入 ``data/secrets.json``）。

    参数:
        key: 传入 ``None`` 或空串表示清除已保存的 Key。
    """
    ensured = ensure_data_dir()
    path = ensured / "secrets.json"
    data = _read_json(path)
    data.setdefault("_comment", "本文件保存本地凭据，已被 .gitignore 忽略；也可以改用环境变量 QIMIND_DEEPSEEK_KEY")
    if key and key.strip():
        data["deepseek_api_key"] = key.strip()
    else:
        data.pop("deepseek_api_key", None)
        data.pop("api_key", None)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def engine_path() -> Optional[Path]:
    """解析 Pikafish 可执行文件路径，找不到时返回 ``None``。"""
    configured = load_settings().get("engine_path")
    if configured:
        path = Path(str(configured))
        if path.is_file():
            return path
    for candidate in ENGINE_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def cache_dir(*parts: str) -> Path:
    """返回（并按需创建）缓存子目录。"""
    path = CACHE_DIR.joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_cchess_importable() -> Path:
    """保证 ``import cchess`` 指向本仓库内的 cchess 源码。

    仓库根目录下也有一个 ``cchess/`` 目录（存放引擎与文档），它没有
    ``__init__.py``，会被 Python 当成命名空间包而遮蔽真正的库；
    这里把 ``cchess/src`` 插到 ``sys.path`` 最前面来纠正。
    """
    src = str(CCHESS_SRC)
    if src not in sys.path:
        sys.path.insert(0, src)
    return CCHESS_SRC
