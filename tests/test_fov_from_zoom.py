"""Тесты расчёта FOV из фактического зума кадра (focal_len из SRT).

Регрессия, найденная при разборе вопроса из чата ПСР про погрешность оценки
координат: FOV был жёстко зафиксирован (camera_hfov_deg=84) на ВСЕ кадры,
хотя в реальных данных focal_len гуляет от 48 до 960 (20-кратный зум) и 87%
детекций сняты с зумом. Для объекта в ЦЕНТРЕ кадра это ни на что не влияло,
но у края кадра давало расхождение до ~1.9 км.

Значения по умолчанию -- под DJI M30T (zoom-камера, файлы _Z), на которой
снимали в этой операции."""
import math

import sar_common


def test_focal_len_is_interpreted_as_tenths_of_mm():
    # focal_len: 240.70 в SRT -> 24.07 мм физического фокусного
    hfov, _ = sar_common.fov_from_focal_len(240.7)
    expected = 2 * math.degrees(math.atan(6.4 / (2 * 24.07)))
    assert abs(hfov - expected) < 0.01


def test_wider_zoom_gives_wider_fov():
    wide, _ = sar_common.fov_from_focal_len(48.0)     # 4.8 мм -- самый широкий
    mid, _ = sar_common.fov_from_focal_len(240.7)     # 24.07 мм
    tele, _ = sar_common.fov_from_focal_len(960.0)    # 96 мм -- максимальный зум
    assert wide > mid > tele
    # порядок величин из реальных данных: ~67 / ~15 / ~3.8 градуса
    assert 60 < wide < 75
    assert 10 < mid < 20
    assert 2 < tele < 6


def test_vertical_fov_is_narrower_than_horizontal_for_non_square_sensor():
    # раньше vertical_fov приравнивался к horizontal -- для матрицы 6.4x4.8
    # это заведомо неверно и напрямую влияет на дистанцию (вертикальное
    # смещение бокса -> угол от надира)
    hfov, vfov = sar_common.fov_from_focal_len(240.7)
    assert vfov < hfov
    expected_v = 2 * math.degrees(math.atan(4.8 / (2 * 24.07)))
    assert abs(vfov - expected_v) < 0.01


def test_returns_none_for_missing_or_nonsense_focal_len():
    assert sar_common.fov_from_focal_len(None) is None
    assert sar_common.fov_from_focal_len(0) is None
    assert sar_common.fov_from_focal_len(-5) is None
    assert sar_common.fov_from_focal_len("не число") is None


def test_sensor_size_is_configurable():
    # другая камера -> другой FOV при том же фокусном
    narrow, _ = sar_common.fov_from_focal_len(240.7, sensor_width_mm=6.4)
    wide, _ = sar_common.fov_from_focal_len(240.7, sensor_width_mm=36.0)
    assert wide > narrow


# --- resolve_frame_fov: приоритеты и резерв ---

def test_resolve_uses_focal_len_when_available():
    hfov, vfov = sar_common.resolve_frame_fov(240.7, {"camera_hfov_deg": 84.0})
    assert hfov < 20  # не 84 -- взят реальный зум
    assert vfov < hfov


def test_resolve_falls_back_to_fixed_hfov_when_focal_len_missing():
    hfov, vfov = sar_common.resolve_frame_fov(None, {"camera_hfov_deg": 84.0})
    assert hfov == 84.0
    assert vfov == 84.0  # резервный путь, грубее -- но лучше, чем ничего


def test_resolve_can_be_disabled_by_config():
    # аварийный выключатель, если для чужой камеры расчёт по focal_len
    # окажется неверным -- поведение вернётся к старому фиксированному FOV
    hfov, _ = sar_common.resolve_frame_fov(240.7, {"camera_hfov_deg": 84.0,
                                                    "use_focal_len_fov": False})
    assert hfov == 84.0


def test_resolve_respects_custom_sensor_and_scale():
    cfg = {"camera_sensor_width_mm": 36.0, "camera_sensor_height_mm": 24.0,
           "focal_len_scale": 1.0, "camera_hfov_deg": 84.0}
    hfov, vfov = sar_common.resolve_frame_fov(50.0, cfg)  # 50 мм на полном кадре
    expected_h = 2 * math.degrees(math.atan(36.0 / (2 * 50.0)))
    assert abs(hfov - expected_h) < 0.01
    assert vfov < hfov


# --- влияние на саму оценку координат ---

def test_zoom_aware_fov_changes_estimate_only_off_center():
    """Ключевая проверка сути бага: для объекта В ЦЕНТРЕ кадра учёт зума
    ничего не меняет (смещение от оптической оси = 0), а у края кадра --
    меняет сильно. Именно так баг и проявлялся: ошибка росла от центра
    к краям."""
    common = dict(drone_lat=39.48, drone_lon=73.59, altitude_m=1147.8,
                  gimbal_yaw_deg=120.0, gimbal_pitch_deg=-22.6,
                  max_distance_m=100000.0)  # снимаем ограничение, сравниваем чистую геометрию

    center_fixed = sar_common.estimate_ground_point(
        bbox_center_frac_x=0.5, bbox_center_frac_y=0.5,
        horizontal_fov_deg=84.0, vertical_fov_deg=84.0, **common)
    center_zoom = sar_common.estimate_ground_point(
        bbox_center_frac_x=0.5, bbox_center_frac_y=0.5,
        horizontal_fov_deg=15.1, vertical_fov_deg=11.4, **common)
    assert abs(center_fixed[2] - center_zoom[2]) < 1.0  # центр -- разницы нет

    edge_fixed = sar_common.estimate_ground_point(
        bbox_center_frac_x=0.9, bbox_center_frac_y=0.9,
        horizontal_fov_deg=84.0, vertical_fov_deg=84.0, **common)
    edge_zoom = sar_common.estimate_ground_point(
        bbox_center_frac_x=0.9, bbox_center_frac_y=0.9,
        horizontal_fov_deg=15.1, vertical_fov_deg=11.4, **common)
    # у края расхождение должно быть сотни метров и больше
    assert abs(edge_fixed[2] - edge_zoom[2]) > 300.0
