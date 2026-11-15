"""Страница находки: кадр целиком и рамка на месте при любых пропорциях.

15.11.2026: вертикальный кадр с телефона выводился в натуральную величину и
обрезался почти наполовину -- рамка уходила за нижний край, хотя подпись
обещала «кадр целиком». Поле комментария было шире колонки, и страница
прокручивалась вбок. Проверено в браузере на кадрах 16:9 и 9:16; здесь --
страж на то, что легко сломать обратно.
"""
import sar_server

PAGE = sar_server.FINDING_FRAME_HTML


def test_frame_block_is_sized_by_image_proportions():
    fit = PAGE[PAGE.index("function fitView()"):PAGE.index("document.getElementById('shot').addEventListener('load', fitView)")]
    for must in ("naturalWidth / img.naturalHeight", "zoom.style.width", "zoom.style.height",
                 "zoom.style.left", "zoom.style.top"):
        assert must in fit, must


def test_zoom_counts_from_the_frame_not_the_window():
    z = PAGE[PAGE.index("function zoomAt("):PAGE.index("view.addEventListener('wheel'")]
    assert "zoom.offsetLeft" in z and "zoom.offsetTop" in z


def test_layout_does_not_overflow_sideways():
    assert "grid-template-columns:minmax(0,1fr) 330px" in PAGE
    assert ".cmt-form textarea {{ box-sizing:border-box; width:100%;" in PAGE
