from __future__ import annotations

from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import Callable
from urllib.parse import urlsplit
from pydantic import BaseModel
from .config import Config, origin


@dataclass(frozen=True)
class Project:
    id: str
    origin: str
    code_url: str


@dataclass(frozen=True)
class Task:
    id: str
    project: str
    input_model: type[BaseModel]
    instructions: str
    render_input: Callable[[BaseModel], str]
    parse_output: Callable[[str], object] = str
    max_input_bytes: int = 4096
    max_output_tokens: int = 512


class Registry:
    """Operator-created immutable task definitions; the default registry is empty."""
    def __init__(self, config: Config, projects=(), tasks=()):
        project_map = {}
        for item in projects:
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", item.id) or item.id in project_map:
                raise ValueError("Project identifiers must be unique slugs.")
            config.check_site(item.origin)
            if origin(item.origin) == config.public_origin:
                raise ValueError("Use a separate origin for preview code and the key popup.")
            url = urlsplit(item.code_url)
            if url.scheme != "https" or url.netloc != "github.com" or not re.fullmatch(r"/[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]+", url.path) or url.query or url.fragment:
                raise ValueError("Code links must identify an explicit GitHub repository.")
            project_map[item.id] = Project(item.id, origin(item.origin), item.code_url)
        task_map = {}
        for item in tasks:
            if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", item.id) or item.id in task_map or item.project not in project_map:
                raise ValueError("Task identifiers must be unique and belong to a project.")
            if not issubclass(item.input_model, BaseModel) or item.input_model.model_config.get("extra") != "forbid":
                raise ValueError("Task input models must forbid extra fields.")
            if (type(item.max_input_bytes) is not int or type(item.max_output_tokens) is not int
                    or not 1 <= item.max_input_bytes <= 8192 or not 16 <= item.max_output_tokens <= 1024):
                raise ValueError("Task bounds exceed gateway limits.")
            if not item.instructions or len(item.instructions.encode()) > 4096:
                raise ValueError("Task instructions exceed the gateway bound.")
            task_map[item.id] = item
        self.projects = MappingProxyType(project_map)
        self.tasks = MappingProxyType(task_map)

    @property
    def origins(self):
        return {project.origin for project in self.projects.values()}


def default_registry(config: Config) -> Registry:
    return Registry(config)  # Real feature adapters are registered by the operator.
