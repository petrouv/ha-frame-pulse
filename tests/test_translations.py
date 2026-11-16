"""Translation files: the Russian one mirrors the English one, which covers the flows."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

TRANSLATIONS = Path(__file__).parent.parent / "custom_components/frame_pulse/translations"


def leaves(node: object, prefix: str = "") -> dict[str, str]:
    if isinstance(node, dict):
        result: dict[str, str] = {}
        for key, value in node.items():
            result |= leaves(value, f"{prefix}.{key}" if prefix else key)
        return result
    return {prefix: node}


def load(language: str) -> dict[str, str]:
    return leaves(json.loads((TRANSLATIONS / f"{language}.json").read_text()))


def test_russian_has_the_same_keys_and_placeholders():
    english, russian = load("en"), load("ru")
    assert set(russian) == set(english)
    for key, text in english.items():
        assert set(re.findall(r"{\w+}", russian[key])) == set(re.findall(r"{\w+}", text)), key


CONFIG_FLOW = TRANSLATIONS.parent / "config_flow.py"


def error_keys(node: ast.AST) -> set[str]:
    """Collect the error keys a flow can set, from its source.

    A key is set directly (`errors[...] = "key"`) or returned by a helper
    together with a value (`return data, "key"`, or `value, error = ..., "key"`).
    """
    keys: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Assign) and any(
            isinstance(target, ast.Subscript)
            and isinstance(target.value, ast.Name)
            and target.value.id == "errors"
            for target in child.targets
        ):
            values = [child.value]
        elif isinstance(child, ast.Return | ast.Assign) and isinstance(child.value, ast.Tuple):
            values = child.value.elts[-1:]
        else:
            continue
        keys |= {
            value.value
            for value in values
            if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value
        }
    return keys


def test_every_error_the_flows_set_is_translated():
    module = ast.parse(CONFIG_FLOW.read_text())
    (options_flow,) = [
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "FramePulseOptionsFlow"
    ]
    rest = [node for node in module.body if node is not options_flow]
    config_flow = ast.Module(body=rest, type_ignores=[])
    english = json.loads((TRANSLATIONS / "en.json").read_text())
    assert error_keys(config_flow) == set(english["config"]["error"])
    assert error_keys(options_flow) == set(english["options"]["error"])


def test_every_form_section_is_translated():
    steps = json.loads((TRANSLATIONS / "en.json").read_text())["config"]["step"]
    fields = {"api": {"username", "password"}, "rtsp": {"port", "username", "password"}}
    expected = {"user": ["api"], "stream": ["rtsp"], "reconfigure": ["api", "rtsp"]}
    for step, sections in expected.items():
        assert list(steps[step]["sections"]) == sections, step
        for name in sections:
            text = steps[step]["sections"][name]
            assert text["name"], (step, name)
            assert set(text["data"]) == fields[name], (step, name)
            assert set(text["data_description"]) == fields[name], (step, name)


def test_api_hint_names_the_loopback_address():
    """go2rtc skips its API login by source address: only loopback, not the same machine.

    A hint that says "the same machine" leads a user who types the LAN address
    of that machine to leave the login out and get `invalid_auth`.
    """
    for language in ("en", "ru"):
        steps = json.loads((TRANSLATIONS / f"{language}.json").read_text())["config"]["step"]
        for step in ("user", "reconfigure"):
            hint = steps[step]["sections"]["api"]["description"]
            assert "127.0.0.1" in hint, (language, step)
            assert "local_auth" in hint, (language, step)
            # Through a port published from a Docker container, 127.0.0.1 typed on the
            # host reaches go2rtc from the Docker network, so go2rtc asks.
            assert "-p 1984:1984" in hint, (language, step)


def test_reconfigure_explains_how_to_drop_a_saved_api_password():
    """An empty password keeps the saved one, so going to `api.username` alone takes two saves."""
    steps = {"en": "open Reconfigure again", "ru": "снова откройте перенастройку"}
    for language, phrase in steps.items():
        step = json.loads((TRANSLATIONS / f"{language}.json").read_text())["config"]["step"]
        hint = step["reconfigure"]["sections"]["api"]["data_description"]["password"]
        assert phrase in hint, language
