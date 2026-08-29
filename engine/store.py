#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
store.py — data/state.json と data/events.json の読み書き。

  state.json  : ボットの記憶。毎回まるごと上書き（原子的に）
  events.json : 追記専用のイベントログ。既存の要素は決して書き換えない・消さない（CLAUDE.md 絶対ルール）
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def fresh_state() -> dict:
    return {"updated": None, "watching": {}, "archived": {}}


def load_state(path: Path) -> dict:
    path = Path(path)
    if not path.exists():
        return fresh_state()
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"state.json が壊れています（dict でない）: {path}")
    return {
        "updated": raw.get("updated"),
        "watching": dict(raw.get("watching") or {}),
        "archived": dict(raw.get("archived") or {}),
    }


def _json_default(o):
    if hasattr(o, "item"):          # numpy スカラー → Python の数値
        return o.item()
    raise TypeError(f"JSON にできない型: {type(o).__name__}")


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=_json_default) + "\n"


def _atomic_write(path: Path, text: str) -> None:
    """一時ファイルに書いてから置き換える（途中で落ちても壊れたファイルを残さない）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_state(path: Path, state: dict) -> None:
    _atomic_write(path, dumps(state))


def load_events(path: Path) -> list:
    path = Path(path)
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"events.json が壊れています（list でない）: {path}")
    return raw


def append_events(path: Path, new_events: list) -> list:
    """既存イベントを一切変えずに末尾へ追加する。id が既にあるものは捨てる。追加した分を返す。

    ファイルが無ければ空リストで作る（Phase 2 の画面が fetch できるように）。
    """
    path = Path(path)
    existing = load_events(path)
    seen = {e["id"] for e in existing}
    added = []
    for e in new_events:
        if e["id"] in seen:
            print(f"[events] duplicate id skipped: {e['id']}")
            continue
        seen.add(e["id"])
        added.append(e)
    if added or not path.exists():
        _atomic_write(path, dumps(existing + added))
    return added
