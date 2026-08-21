"""Комментарии в плеере: обычный разговор вместо приклеенного виджета.

Жалоба была про UI, но при разборе нашлась причина серьёзнее внешнего
вида. Список наблюдений и список сцен перерисовываются целиком через
innerHTML КАЖДЫЕ 15 СЕКУНД, а поле ввода комментария живёт внутри этих
списков. Значит набранный текст, фокус и позиция курсора исчезали ровно
посреди фразы -- дописать длинную мысль было почти невозможно.

Остальное -- вид. Блок жил в собственной палитре (синие ссылки #8ecbff,
зелёные имена #9fe8b5, синяя рамка кнопки #3355aa) на странице, где акцент
платформы бирюзовый, а вход в обсуждение выглядел переключателем
"💬 обсуждение (2)", то есть органом управления, а не началом разговора.
"""
import re

import pytest

import sar_server


PLAYER = sar_server.PLAYER_PAGE_HTML


def css_block():
    """Стили комментариев БЕЗ комментариев в коде.

    Пояснение к правкам само упоминает старые цвета -- если не вырезать
    комментарии, проверка на них сработает на объяснении, а не на стиле.
    """
    i = PLAYER.index(".discussion {{")
    j = PLAYER.index(".cmt-locked")
    block = PLAYER[i:PLAYER.index("}}", j) + 2]
    return re.sub(r"/\*.*?\*/", "", block, flags=re.S)


# --- главное: набранный текст не пропадает --------------------------------

def test_lists_are_not_rerendered_while_someone_is_typing():
    """Причина, по которой обсуждение было неудобным."""
    assert "function isTypingComment" in PLAYER
    assert "function setCardsHtml" in PLAYER
    # оба списка с обсуждениями внутри обязаны идти через защиту
    assert PLAYER.count("setCardsHtml(list") >= 4, (
        "какой-то из списков всё ещё перерисовывается напрямую и снесёт "
        "поле ввода вместе с набранным текстом")


def test_no_direct_innerhtml_on_lists_with_comments():
    for bad in ("list.innerHTML = observations.map", "list.innerHTML = scenes.map"):
        assert bad not in PLAYER, (
            f"«{bad}» перерисовывает карточки напрямую, минуя защиту")


def test_draft_survives_a_rerender():
    """Отвлечься на видео посреди фразы -- нормально. Фокус уйдёт, и тогда
    перерисовка законна; текст терять всё равно нельзя."""
    assert "commentDrafts" in PLAYER
    assert "commentDrafts[ta.id] = ta.value" in PLAYER, "черновик не сохраняется"
    assert "const draft = commentDrafts[inputId]" in PLAYER, (
        "черновик сохраняется, но при перерисовке не подставляется обратно")


def test_draft_is_escaped_when_put_back():
    """Черновик -- это текст, набранный человеком, и он попадает обратно
    в разметку."""
    assert "escapeHtml(draft)" in PLAYER


def test_deferred_rerender_is_caught_up_later():
    """Иначе список останется устаревшим до следующего тика."""
    assert "pendingCardRerender" in PLAYER
    assert "focusout" in PLAYER


def test_open_discussion_stays_open_after_rerender():
    assert "openDiscussions" in PLAYER
    assert "rememberDiscussion" in PLAYER


# --- форма ----------------------------------------------------------------

def test_placeholder_is_exactly_what_was_asked():
    assert 'placeholder="Ваш комментарий"' in PLAYER


def test_send_is_disabled_until_there_is_text():
    """Пустая отправка не должна молча отсекаться внутри обработчика --
    кнопка просто неактивна, и это видно."""
    assert "btn.disabled = !ta.value.trim()" in PLAYER


def test_ctrl_enter_sends_and_plain_enter_does_not():
    """Пометки бывают в несколько предложений -- обычный Enter обязан
    оставлять перенос строки."""
    assert "event.ctrlKey || event.metaKey" in PLAYER
    assert "Ctrl+Enter" in PLAYER


def test_failed_send_keeps_the_text():
    """Если запрос не прошёл, текст должен остаться в поле, а человек --
    узнать об этом. Молча проглоченная ошибка означает, что он считает
    сообщение отправленным, а его нет."""
    src = PLAYER[PLAYER.index("async function addComment"):]
    src = src[:src.index("async function deleteComment")]
    assert "if (!res.ok) throw" in src, "ошибка сервера не замечается"
    assert src.index("if (!res.ok) throw") < src.index("ta.value = ''"), (
        "поле очищается до того, как стало известно, что отправка удалась")
    assert src.index("if (!res.ok) throw") < src.index("delete commentDrafts"), (
        "черновик стирается раньше, чем подтвердилась отправка")
    assert "catch (e) {}" not in src, "глухой catch"


# --- вид ------------------------------------------------------------------

def test_discussion_header_is_a_caption_not_a_toggle():
    """Было «💬 обсуждение (2)» -- эмодзи-переключатель. Стало подписью."""
    assert "Комментарии ·" in PLAYER
    assert "Добавить комментарий" in PLAYER
    assert "list-style:none" in css_block(), "остался треугольник-маркер"


def test_comments_use_the_platform_palette():
    """Плеер и страницы операций должны выглядеть одной системой."""
    block = css_block()
    assert "--c-accent:#5fb8c7" in block, "акцент не совпадает с платформой"
    for alien in ("#8ecbff", "#9fe8b5", "#3355aa"):
        assert alien not in block, (
            f"в стилях комментариев остался чужой цвет {alien}")


def test_replies_are_not_boxed():
    """Десяток реплик в рамочках читается как стопка карточек, а не как
    разговор: разделять их должен воздух."""
    block = css_block()
    m = re.search(r"\.cmt \{\{([^}]*)\}\}", block)
    assert m, "не найдено правило реплики"
    assert "border:" not in m.group(1)
    assert "background:" not in m.group(1)


def test_delete_button_appears_on_hover():
    """Постоянный ряд крестиков превращает разговор в панель управления."""
    block = css_block()
    assert ".cmt:hover .cmt-del" in block
    m = re.search(r"\.cmt-del \{\{([^}]*)\}\}", block)
    assert "opacity:0" in m.group(1)


def test_input_is_quiet_until_focused():
    """Просили неяркое и неброское: акцент загорается только при работе."""
    block = css_block()
    assert "textarea:focus" in block and "border-color:var(--c-accent)" in block
    m = re.search(r"\.cmt-form button \{\{([^}]*)\}\}", block)
    assert "background:transparent" in m.group(1), "кнопка кричит фоном"


# --- страница по-прежнему собирается --------------------------------------

def test_player_page_still_renders():
    fields = set(re.findall(r"(?<!\{)\{([a-z_][a-z0-9_]*)\}(?!\})", PLAYER))
    html = PLAYER.format(**{f: "X" for f in fields})
    assert 'placeholder="Ваш комментарий"' in html
    assert len(html) > 10000


@pytest.mark.parametrize("pair", [("{{", "}}"), ("(", ")"), ("[", "]")])
def test_script_brackets_are_balanced(pair):
    """Node в системе нет, поэтому хотя бы грубая проверка: резкий перекос
    скобок после правки шаблона видно сразу."""
    fields = set(re.findall(r"(?<!\{)\{([a-z_][a-z0-9_]*)\}(?!\})", PLAYER))
    html = PLAYER.format(**{f: "X" for f in fields})
    js = html[html.index("<script>"):html.rindex("</script>")]
    a, b = ("{", "}") if pair[0] == "{{" else pair
    assert js.count(a) == js.count(b), f"скобки {a}{b} разошлись"
