"""Тесты для sar_common.estimate_ground_point() -- грубой геометрической
оценки координат объекта в кадре по позиции дрона + углам подвеса + FOV.

Это НЕ точное измерение (см. предупреждения в docstring функции), поэтому
тесты проверяют геометрию на контрольных, легко проверяемых руками случаях
(прямо вниз, известный угол, вне диапазона), а не гоняются за
"реалистичностью" произвольных чисел."""
import math

import pytest

from sar_common import estimate_ground_point


def test_straight_down_gives_zero_distance_at_drone_position():
    # gimbal_pitch=-90 (надир), пиксель точно в центре кадра -- луч идёт
    # прямо вниз, объект должен оказаться ровно под дроном
    result = estimate_ground_point(
        drone_lat=42.0, drone_lon=74.0, altitude_m=500,
        gimbal_yaw_deg=0, gimbal_pitch_deg=-90,
        bbox_center_frac_x=0.5, bbox_center_frac_y=0.5,
        horizontal_fov_deg=84, vertical_fov_deg=84)
    assert result is not None
    est_lat, est_lon, dist = result
    assert dist == pytest.approx(0.0, abs=1e-9)
    assert est_lat == pytest.approx(42.0, abs=1e-9)
    assert est_lon == pytest.approx(74.0, abs=1e-9)


def test_known_45_degree_angle_north_bearing():
    # 45° от надира на высоте 1000 м -> горизонтальное расстояние = 1000 м
    # (tan(45°) = 1), азимут 0° (север) -> смещение только по широте
    altitude = 1000.0
    result = estimate_ground_point(
        drone_lat=42.0, drone_lon=74.0, altitude_m=altitude,
        gimbal_yaw_deg=0, gimbal_pitch_deg=-45,
        bbox_center_frac_x=0.5, bbox_center_frac_y=0.5,
        horizontal_fov_deg=84, vertical_fov_deg=84)
    assert result is not None
    est_lat, est_lon, dist = result
    assert dist == pytest.approx(1000.0, rel=1e-6)
    expected_delta_lat_deg = math.degrees(1000.0 / 6371000.0)
    assert est_lat == pytest.approx(42.0 + expected_delta_lat_deg, abs=1e-6)
    assert est_lon == pytest.approx(74.0, abs=1e-6)  # sin(0°) = 0 -- долгота не меняется


def test_bottom_edge_pixel_pushes_angle_further_toward_nadir():
    # пиксель у нижнего края кадра (frac_y=1.0) с vfov=90 добавляет ещё 45°
    # вниз к базовому pitch=-45 -> суммарно -90 (надир) -> расстояние 0
    result = estimate_ground_point(
        drone_lat=42.0, drone_lon=74.0, altitude_m=1000,
        gimbal_yaw_deg=0, gimbal_pitch_deg=-45,
        bbox_center_frac_x=0.5, bbox_center_frac_y=1.0,
        horizontal_fov_deg=90, vertical_fov_deg=90)
    assert result is not None
    _, _, dist = result
    assert dist == pytest.approx(0.0, abs=1e-6)


def test_right_edge_pixel_shifts_bearing_east_of_north():
    # base yaw=0 (север), пиксель у правого края (frac_x=1.0) с hfov=90
    # добавляет +45° к азимуту -> итоговый азимут 45° (северо-восток) ->
    # объект смещается и по широте, и по долготе (обе положительные)
    result = estimate_ground_point(
        drone_lat=42.0, drone_lon=74.0, altitude_m=1000,
        gimbal_yaw_deg=0, gimbal_pitch_deg=-45,
        bbox_center_frac_x=1.0, bbox_center_frac_y=0.5,
        horizontal_fov_deg=90, vertical_fov_deg=90)
    assert result is not None
    est_lat, est_lon, dist = result
    assert est_lat > 42.0
    assert est_lon > 74.0  # к востоку


def test_camera_pointed_above_horizon_returns_none():
    # pitch=0 (горизонт), без смещения пикселя -> угол от надира ровно 90° ->
    # луч не пересекает землю в разумных пределах
    result = estimate_ground_point(
        drone_lat=42.0, drone_lon=74.0, altitude_m=1000,
        gimbal_yaw_deg=0, gimbal_pitch_deg=0,
        bbox_center_frac_x=0.5, bbox_center_frac_y=0.5,
        horizontal_fov_deg=84, vertical_fov_deg=84)
    assert result is None


def test_near_horizon_angle_does_not_produce_absurd_distance():
    """РЕГРЕССИЯ: найдено на реальных данных. При угле от надира, близком к
    90° (почти горизонт), tan() численно взрывается -- формально угол ещё
    "меньше 90°" (проходит существующую проверку), но результат -- десятки
    километров, что физически бессмысленно для объекта, видимого в кадре
    дрона. Реальный случай: pitch~-8.7°, altitude~982.557 м, после смещения
    bbox эффективный угол от надира ~89.19° -> дало 69.8 КМ вместо разумной
    оценки. Теперь max_distance_m должен отсекать такой "мусорный" результат."""
    result = estimate_ground_point(
        drone_lat=39.485702, drone_lon=73.595223, altitude_m=982.557,
        gimbal_yaw_deg=-143.7, gimbal_pitch_deg=-8.7,
        # такое смещение bbox даёт итоговый угол от надира ~89.19° --
        # тот самый случай, что дал 69.8 км в реальном отчёте (проверено
        # напрямую: без ограничения max_distance_m эти же аргументы дают
        # ~70015 м, совпадает с наблюдённым 69830.33 м с точностью до
        # неточности округления реальных входных телеметрических данных)
        bbox_center_frac_x=0.5, bbox_center_frac_y=0.406,
        horizontal_fov_deg=84.0)
    assert result is None


def test_distance_just_under_cap_is_kept_just_over_is_rejected():
    # 45° от надира на высоте 1000 м -> ровно 1000 м -- должно пройти при
    # дефолтном max_distance_m=3000
    ok = estimate_ground_point(
        drone_lat=42.0, drone_lon=74.0, altitude_m=1000,
        gimbal_yaw_deg=0, gimbal_pitch_deg=-45,
        bbox_center_frac_x=0.5, bbox_center_frac_y=0.5,
        horizontal_fov_deg=84, vertical_fov_deg=84)
    assert ok is not None

    # тот же угол, но с явным жёстким потолком меньше 1000 м -- должно отсечься
    capped = estimate_ground_point(
        drone_lat=42.0, drone_lon=74.0, altitude_m=1000,
        gimbal_yaw_deg=0, gimbal_pitch_deg=-45,
        bbox_center_frac_x=0.5, bbox_center_frac_y=0.5,
        horizontal_fov_deg=84, vertical_fov_deg=84, max_distance_m=500)
    assert capped is None


def test_missing_telemetry_field_returns_none():
    result = estimate_ground_point(
        drone_lat=42.0, drone_lon=74.0, altitude_m=1000,
        gimbal_yaw_deg=None, gimbal_pitch_deg=-45,
        bbox_center_frac_x=0.5, bbox_center_frac_y=0.5,
        horizontal_fov_deg=84, vertical_fov_deg=84)
    assert result is None


def test_non_positive_altitude_returns_none():
    result = estimate_ground_point(
        drone_lat=42.0, drone_lon=74.0, altitude_m=0,
        gimbal_yaw_deg=0, gimbal_pitch_deg=-45,
        bbox_center_frac_x=0.5, bbox_center_frac_y=0.5,
        horizontal_fov_deg=84, vertical_fov_deg=84)
    assert result is None


def test_vertical_fov_defaults_to_horizontal_when_not_given():
    with_explicit = estimate_ground_point(
        drone_lat=42.0, drone_lon=74.0, altitude_m=1000,
        gimbal_yaw_deg=0, gimbal_pitch_deg=-45,
        bbox_center_frac_x=0.5, bbox_center_frac_y=0.8,
        horizontal_fov_deg=84, vertical_fov_deg=84)
    with_default = estimate_ground_point(
        drone_lat=42.0, drone_lon=74.0, altitude_m=1000,
        gimbal_yaw_deg=0, gimbal_pitch_deg=-45,
        bbox_center_frac_x=0.5, bbox_center_frac_y=0.8,
        horizontal_fov_deg=84)
    assert with_explicit == with_default
