"""Регрессионный тест: report.html (сцены) должен содержать ссылку "к списку
файлов", когда открыт через сервер (переданы tracking_report_id/
tracking_api_base) -- баг: раньше такой ссылки не было вообще, в отличие от
страницы обработки и плеера, у которых она уже была."""
from sar_video_review import Hit, group_hits_into_scenes, save_outputs


def _make_hit():
    return Hit(
        frame_idx=0, timestamp="0:00:00", seconds=0.0, confidence=0.9,
        object_class="person", source="model", bbox=[10, 10, 50, 50],
        lat=None, lon=None, alt=None, image_path="c.jpg", full_image_path=None,
    )


def test_report_html_has_home_link_when_served_via_server(tmp_path):
    hits = [_make_hit()]
    groups = group_hits_into_scenes(hits, max_frame_gap=100, distance_multiplier=2.5)
    save_outputs(hits, groups, str(tmp_path), "video.mp4",
                  tracking_report_id="rep1", tracking_api_base="http://127.0.0.1:8080")
    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert '<a href="/">' in html
    assert "к списку файлов" in html


def test_report_html_has_no_home_link_when_opened_standalone(tmp_path):
    hits = [_make_hit()]
    groups = group_hits_into_scenes(hits, max_frame_gap=100, distance_multiplier=2.5)
    save_outputs(hits, groups, str(tmp_path), "video.mp4")  # без tracking_* -- как в CLI без сервера
    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    # ссылка на "/" была бы переходом в никуда без сервера -- не должно быть вовсе
    assert '<a href="/">' not in html
