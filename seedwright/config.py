"""Configuration helpers for dialect connection settings."""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass


DEFAULT_CONFIG_PATH = "seedwright.ini"


@dataclass(frozen=True)
class DialectConfig:
    """A named section from the user config file."""

    name: str
    values: dict[str, str]

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.values.get(key, default)


def load_config(path: str | None = DEFAULT_CONFIG_PATH) -> configparser.ConfigParser:
    """Load a seedwright config file.

    The default config is optional so SQLite-only workflows do not need one.
    An explicitly supplied missing path is treated as an error.
    """
    parser = configparser.ConfigParser()
    if path is None:
        return parser
    if os.path.exists(path):
        parser.read(path)
        return parser
    if path != DEFAULT_CONFIG_PATH:
        raise FileNotFoundError(f"config file not found: {path}")
    return parser


def dialect_config(
    parser: configparser.ConfigParser,
    dialect: str,
) -> DialectConfig:
    if parser.has_section(dialect):
        return DialectConfig(dialect, dict(parser[dialect]))
    return DialectConfig(dialect, {})


def config_value(
    config: DialectConfig,
    key: str,
    override: str | None = None,
) -> str | None:
    return override if override is not None else config.get(key)


def app_config(parser: configparser.ConfigParser) -> DialectConfig:
    """Return global seedwright settings from the [seedwright] section."""
    return dialect_config(parser, "seedwright")
