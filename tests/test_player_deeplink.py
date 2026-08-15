"""Регрессионный тест: из report.html (карточка сцены, полноэкранный кадр)
должна быть ссылка "провалиться" в ручной плеер сразу на нужный таймкод
(?t=<секунды>), а сам плеер должен на неё реагировать (JS seekFromUrl в
sar_server.py PLAYER_PAGE_HTML). Ссылка имеет смысл только когда отчёт открыт
через сервер -- без него /report/.../player/ не существует."""
import json
import re

from sar_video_review import Hit, group_hits_into_scenes, save_outputs

import sar_server


def _make_hit(frame_idx=0, seconds=0.0):
    return Hit(
        frame_idx=frame_idx, timestamp=f"0:00:{int(seconds):02d}", seconds=seconds,
        confidence=0.9, object_class="person", source="model", bbox=[10, 10, 50, 50],
        lat=None, lon=None, alt=None, image_path="c.jpg", full_image_path="full.jpg",
    )


def test_report_html_has_player_deeplinks_when_served_via_server(tmp_path):
    hits = [_make_hit(0, 12.5), _make_hit(1, 13.0)]
    groups = group_hits_into_scenes(hits, max_frame_gap=100, distance_multiplier=2.5)
    save_outputs(hits, groups, str(tmp_path), "video.mp4",
                  tracking_report_id="rep1", tracking_api_base="http://127.0.0.1:8080")
    html = (tmp_path / "report.html").read_text(encoding="utf-8")

    # PLAYER_BASE -- относительный URL плеера ЭТОГО отчёта
    assert 'const PLAYER_BASE = "/report/rep1/player/";' in html
    # ссылка на карточке сцены (openScene) и на кадре (openFull) строится через один и тот же хелпер
    assert "playerLinkHtml(g.time_start_sec)" in html
    assert "playerLinkHtml(h.seconds)" in html

    # у каждого hit в сцене должен быть "seconds", чтобы ссылка на конкретный
    # кадр указывала на его собственный таймкод, а не на начало сцены
    m = re.search(r'id="scene-data"[^>]*>(.+?)</script>', html, re.S)
    scene_json = json.loads(m.group(1))
    group_id = next(iter(scene_json))
    for h in scene_json[group_id]["hits"]:
        assert "seconds" in h


def test_report_html_has_no_player_deeplinks_when_opened_standalone(tmp_path):
    hits = [_make_hit()]
    groups = group_hits_into_scenes(hits, max_frame_gap=100, distance_multiplier=2.5)
    save_outputs(hits, groups, str(tmp_path), "video.mp4")  # без tracking_* -- как в CLI без сервера
    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "const PLAYER_BASE = '';" in html


def test_player_page_js_seeks_from_t_query_param():
    # без реального DB/report -- достаточно убедиться, что шаблон плеера
    # содержит логику чтения ?t= и её нет только "случайно" по опечатке
    assert "seekFromUrl" in sar_server.PLAYER_PAGE_HTML
    assert "URLSearchParams(location.search).get('t')" in sar_server.PLAYER_PAGE_HTML
    assert "video.currentTime = t;" in sar_server.PLAYER_PAGE_HTML
