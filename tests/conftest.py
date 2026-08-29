# -*- coding: utf-8 -*-
"""pytest 共通設定: engine/ を import パスに入れる（テストは `pytest -q tests` で実行）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
