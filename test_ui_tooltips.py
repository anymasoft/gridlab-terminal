# -*- coding: utf-8 -*-
"""У каждого знака вопроса в интерфейсе обязан быть текст.

Три параметра грида — уровни лестницы, нотионал и ре-анкор — рисовали «?»
с пустым data-tip. Человек наводил мышь и не получал ничего: обработчик
показа молча выходит, если текста нет. Такой пропуск невозможно заметить
глазами, поэтому он проверяется тестом.

Запуск: pytest -q test_ui_tooltips.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "app.js")


def _source():
    with open(APP, encoding="utf-8") as f:
        return f.read()


def _tips(src):
    m = re.search(r"const TIPS = \{(.*?)\n\};", src, re.S)
    assert m, "словарь TIPS не найден"
    return {k: True for k in re.findall(r"^\s*([A-Za-z_]\w*)\s*:", m.group(1), re.M)}


def _referenced(src):
    """Ключи, для которых интерфейс рисует знак вопроса или строку параметра."""
    return (set(re.findall(r"qq\('([^']+)'\)", src))
            | set(re.findall(r"paramRow\('[^']*','([^']+)'", src)))


def test_every_referenced_key_has_a_tooltip():
    src = _source()
    missing = sorted(_referenced(src) - set(_tips(src)))
    assert not missing, f"знак вопроса без текста у: {missing}"


def test_tooltips_are_not_empty_strings():
    src = _source()
    m = re.search(r"const TIPS = \{(.*?)\n\};", src, re.S)
    empty = re.findall(r"^\s*([A-Za-z_]\w*)\s*:\s*''", m.group(1), re.M)
    assert not empty, f"пустая подсказка у: {empty}"


def test_grid_parameters_are_all_explained():
    """Именно эти три пропускались. Закрепляем явно, чтобы не вернулось."""
    tips = _tips(_source())
    for key in ("grid_levels", "grid_notional_mult", "grid_reanchor", "leverage"):
        assert key in tips, f"нет подсказки для {key}"


def test_question_mark_is_not_rendered_without_text():
    """Знак вопроса без текста — мёртвый элемент. Его не должно быть вовсе:
    иначе пропуск выглядит как рабочая подсказка, которая почему-то молчит."""
    src = _source()
    m = re.search(r"function qq\(key\)\{(.*?)\n\}", src, re.S)
    assert m, "функция qq не найдена"
    body = m.group(1)
    assert "TIPS[key] ?" in body or "TIPS[key]?" in body, \
        "qq обязана возвращать пустоту, когда текста нет"
    assert "TIPS[key]||''" not in body.replace(" ", ""), \
        "подстановка пустой строки возвращает мёртвый знак вопроса"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  OK {name}")
    print("\nALL OK (подсказки интерфейса)")
