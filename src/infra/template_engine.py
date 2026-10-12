import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from jinja2 import Environment, FileSystemLoader, TemplateSyntaxError, meta

from app.ports import TemplateEnginePort


def _none_to_empty_string(val: Any):
    if val is None:
        return ""
    return val


@dataclass
class TemplateEngineAdapter(TemplateEnginePort):
    templates_path: Path
    base_path: str | None = None

    def __post_init__(self):
        self._engine = Environment(
            loader=FileSystemLoader(self.templates_path),
            finalize=_none_to_empty_string,
            autoescape=True,
        )

    @property
    def engine(self):
        return self._engine

    def get_template_text(self, template_name: str):
        path = f"{self.base_path}/{template_name}" if self.base_path else template_name
        template = self.engine.get_template(name=path)
        with open(file=cast(str, template.filename), mode="r") as file_stream:
            return file_stream.read()

    def render(self, template_name: str, /, *_: Any, **kwargs: Any):
        path = f"{self.base_path}/{template_name}" if self.base_path else template_name
        template = self.engine.get_template(name=path)
        return self._remove_left_whitespace(
            s=self._clean_line_breaks(s=template.render(**kwargs))
        )

    def render_string(self, template_text: str, /, *_: Any, **kwargs: Any):
        template = self.engine.from_string(template_text)
        return template.render(**kwargs)

    def extract_tags_from_string(self, template_text: str):
        ast = self.engine.parse(template_text)
        return tuple(meta.find_undeclared_variables(ast))

    def is_valid(self, template_text: str | None):
        try:
            if template_text:
                self.engine.parse(template_text)
                return True
            raise TemplateSyntaxError(message="No input value", lineno=0)
        except TemplateSyntaxError:
            return False

    def _clean_line_breaks(self, s: str):
        return re.sub(r"^\n+|\n+$", "", s)

    def _remove_left_whitespace(self, s: str):
        return "\n".join(line.lstrip() for line in s.splitlines())
