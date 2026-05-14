"""ASTrea 用户级配置路径与环境变量加载。"""
import os
from pathlib import Path
from typing import List

from dotenv import find_dotenv, load_dotenv


def get_user_config_dir() -> Path:
    """返回 ASTrea 用户级配置目录。"""
    if os.name == "nt":
        base = os.getenv("APPDATA")
        if base:
            return Path(base) / "ASTrea"
        return Path.home() / "AppData" / "Roaming" / "ASTrea"

    base = os.getenv("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "astrea"
    return Path.home() / ".config" / "astrea"


def get_user_env_path() -> Path:
    """返回用户级 .env 路径。"""
    return get_user_config_dir() / ".env"


def find_project_env_path() -> Path | None:
    """从当前工作目录向上查找项目级 .env。"""
    found = find_dotenv(usecwd=True)
    return Path(found) if found else None


def load_astrea_env() -> List[Path]:
    """按优先级加载 ASTrea 配置。

    优先级：当前目录/父目录 .env > 用户级 .env > 系统环境变量。
    """
    loaded: List[Path] = []

    user_env = get_user_env_path()
    if user_env.is_file():
        load_dotenv(user_env, override=True)
        loaded.append(user_env)

    project_env = find_project_env_path()
    if project_env and project_env.is_file():
        load_dotenv(project_env, override=True)
        loaded.append(project_env)

    return loaded


def has_env_file() -> bool:
    """是否存在用户级或项目级 .env 文件。"""
    return get_user_env_path().is_file() or bool(find_project_env_path())
