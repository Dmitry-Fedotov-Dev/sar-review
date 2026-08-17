#!/usr/bin/env python3
"""
SAR Video Review — покадровый анализ записанного видео с дрона:
люди, палатки, рюкзаки, снаряжение + детектор цветовых аномалий.

Отличие от "живого" детектора:
  - работает с файлом целиком (offline), не со стримом
  - режет каждый кадр на перекрывающиеся тайлы -> сильно повышает recall
    на мелких/дальних объектах (человек с высоты дрона = 20-60 px)
  - парсит SRT-телеметрию дрона (GPS/высота), если она есть рядом с видео
  - на выходе не видео, а ГАЛЕРЕЯ КАДРОВ-КАНДИДАТОВ (HTML) с фильтром по
    классам, отсортированная по уверенности — это то, что реально
    отсматривает человек, а не видео целиком

Два независимых источника кандидатов (работают одновременно):
  1. Object detection по классам (--classes person,tent,backpack,...).
     Требует, чтобы модель ЗНАЛА эти классы. Есть два режима:
       --model-type custom     — ваша обученная модель (свои веса .pt)
       --model-type yolo-world — открытый словарь (YOLO-World), детектирует
                                  по текстовым названиям БЕЗ ДООБУЧЕНИЯ.
                                  Практично для классов, которых пока нет
                                  в вашей модели (палатка, ледоруб, каска и т.д.)
  2. Детектор цветовых аномалий (--color-anomaly, включён по умолчанию) —
     без всякой модели ищет пятна ярких "неприродных" цветов (оранжевый,
     красный, жёлтый, синий) — типичные цвета курток/палаток/рюкзаков на
     фоне скал/снега. Для мелких объектов (каска, элементы одежды) часто
     работает надёжнее, чем object detection.

Реалистичная оценка по типам объектов с высоты дрона:
  - палатка       — отлично видна (крупная, яркий цвет) -> оба метода работают хорошо
  - рюкзак/куртка  — видны при ярком цвете -> лучше всего цветовой детектор
  - человек        — работает object detection + тайлинг
  - каска, ледоруб, палки — на высоте десятков-сотен метров это единицы
    пикселей; честно: единственный реалистичный шанс их поймать — это
    случай, когда дрон летит низко/близко, либо они лежат на снегу и дают
    контрастное пятно (тогда сработает цветовой детектор, не object detection)

Как адаптировать под свою модель:
  - если у вас НЕ ultralytics/YOLO, а свой инференс (ONNX/TensorRT/TFLite/
    кастомная обёртка) — измените ТОЛЬКО функцию run_inference_on_tile().
    Она должна принимать BGR-кадр (numpy array) и список нужных классов,
    возвращать [(x1, y1, x2, y2, confidence, class_name), ...] в
    координатах ТАЙЛА. Остальной пайплайн менять не нужно.

Зависимости:
    pip install ultralytics opencv-python numpy --break-system-packages

Запуск (своя модель, только известные ей классы):
    python sar_video_review.py \
        --video DJI_0001.MP4 --model person_detector.pt --model-type custom \
        --classes "person,backpack" \
        --conf 0.15 --tile 640 --overlap 0.25 --frame-skip 2 \
        --out ./review_DJI_0001

Запуск (без дообучения, открытый словарь для палаток/снаряжения):
    python sar_video_review.py \
        --video DJI_0001.MP4 --model yolov8x-worldv2.pt --model-type yolo-world \
        --classes "person,tent,backpack,helmet,tarp,sleeping bag" \
        --conf 0.10 --tile 800 --overlap 0.25 --frame-skip 2 \
        --out ./review_DJI_0001

    После обработки откройте ./review_DJI_0001/report.html — галерея
    кандидатов с чекбоксами фильтрации по классам.
"""

import argparse
import csv
import json
import os
import re
import sys
from dataclasses import dataclass, asdict
from datetime import timedelta

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sar_common


def _limit_cpu_threads(reserve_cores=2):
    """Оставляет часть ядер веб-сервису. Без этого torch/ultralytics и OpenCV
    забирают ВСЕ ядра под инференс (это отмечено в CLAUDE.md как известная
    особенность), и на той же машине веб-интерфейс начинает ощутимо
    подвисать у всех, кто в это время смотрит видео -- поймано на реальной
    работе команды из 6 человек.

    Вызывать нужно ДО импорта torch/ultralytics: переменные окружения
    OMP/MKL читаются один раз при их инициализации."""
    try:
        total = os.cpu_count() or 1
    except Exception:
        total = 1
    threads = max(1, total - max(0, reserve_cores))
    for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                "NUMEXPR_NUM_THREADS"):
        os.environ.setdefault(var, str(threads))
    try:
        cv2.setNumThreads(threads)
    except Exception:
        pass
    return threads


# reserve_cores можно переопределить в sar_config.json (detector_reserve_cores),
# но конфиг читается позже -- поэтому базовое ограничение ставим сразу, а
# точное значение применяется в main() (см. ниже).
_limit_cpu_threads()

# ---------------------------------------------------------------------------
# 0. КОНФИГ — дефолтные значения всех флагов. Если рядом со скриптом лежит
#    sar_config.json — он подхватывается автоматически (можно и явно указать
#    --config путь). БЕЗ конфига поведение скрипта идентично прежнему —
#    новые опции (full_frame_save_mode и т.д.) выставлены так, чтобы ничего
#    не менять по умолчанию.
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "model": "yolov8s-worldv2.pt",
    "model_type": "yolo-world",
    "classes": "person",
    "conf": 0.15,
    "tile": 640,
    "overlap": 0.25,
    "frame_skip": 2,
    # Собственная нарезка кадра на тайлы + NMS (make_tiles/nms в этом файле).
    # Раньше был ещё опциональный бэкенд через библиотеку SAHI, но он
    # работал только поверх ultralytics -- вместе с ним и убран.
    "detector_backend": "custom",
    "color_anomaly": True,
    # потолок на число цветовых пятен, оставляемых на ОДНОМ кадре (самые
    # уверенные). На горном материале детектор без потолка вырождался в шум:
    # ~167 срабатываний на кадр по "синему" (небо и тени в снегу), 503 499
    # штук на одно видео против 12 611 у модели -- отчёт становился на 296 МБ
    # и не открывался. Настоящая находка почти всегда среди самых ярких пятен.
    "color_max_per_frame": 12,
    # сколько ядер CPU оставить веб-сервису, чтобы интерфейс не подвисал у
    # людей, пока на той же машине идёт инференс (см. _limit_cpu_threads)
    "detector_reserve_cores": 2,
    "crop_margin": 60,
    "full_frame_quality": 88,
    # "all"            -> как раньше: полный кадр сохраняется для каждого
    #                     обработанного кадра, где есть детекция
    # "representative" -> экономия диска: после группировки на сцены
    #                     оставляются только 1-3 представительных полных
    #                     кадра на группу (первый/пиковый/последний),
    #                     остальные удаляются с диска в конце обработки
    "full_frame_save_mode": "all",
    # "lat_lon" | "lon_lat" -- порядок координат в нестандартном формате
    # телеметрии "GPS(a, b, c)" без подписей (см. parse_srt_telemetry).
    # Если координаты в отчётах оказываются перепутаны местами (сверьте
    # с картой) -- поменяйте на противоположное значение.
    "srt_gps_tuple_order": "lat_lon",
    # папка с SRT-телеметрией полётов, рядом с watch_dir (т.е. рядом с самим
    # видео -- videos лежат прямо в корне watch_dir без подпапок). Сканируется
    # РЕКУРСИВНО (в отличие от watch_dir с видео/фото) -- см. sar_common.py.
    # Используется только если рядом с самим видео нет файла videoname.srt
    # (старое поведение имеет приоритет, ничего не ломает).
    "telemetry_dir": sar_common.DEFAULT_TELEMETRY_DIR_NAME,
    # FOV кадра для оценки "вероятных координат" объекта считается из
    # ФАКТИЧЕСКОГО зума каждого кадра (focal_len из SRT) + размеров матрицы --
    # см. sar_common.resolve_frame_fov. Раньше тут было одно фиксированное
    # значение на все кадры, и это давало ошибку до ~1.9 км для объектов у
    # края кадра на сильном зуме (87% реальных детекций сняты с зумом).
    "use_focal_len_fov": True,
    # матрица камеры в мм. По умолчанию -- 1/2" (DJI M30T, zoom-камера, файлы
    # _Z). ДРУГОЙ дрон/камера -> обязательно поменяйте, иначе все оценки
    # координат будут систематически смещены.
    "camera_sensor_width_mm": sar_common.DEFAULT_SENSOR_WIDTH_MM,
    "camera_sensor_height_mm": sar_common.DEFAULT_SENSOR_HEIGHT_MM,
    # множитель перевода focal_len из SRT в мм. У DJI фокусное пишется в
    # десятых долях мм (focal_len: 240.70 -> 24.07 мм).
    "focal_len_scale": sar_common.DEFAULT_FOCAL_LEN_SCALE,
    # РЕЗЕРВ на случай, когда в SRT нет focal_len (или use_focal_len_fov=false):
    # фиксированный горизонтальный угол обзора камеры в градусах. Заведомо
    # грубее, чем расчёт по зуму -- вертикальный FOV при этом приравнивается
    # к горизонтальному, хотя матрица не квадратная.
    "camera_hfov_deg": 84.0,
    # потолок для "вероятных координат" объекта в метрах -- если геометрический
    # расчёт (см. sar_common.estimate_ground_point) даёт расстояние БОЛЬШЕ
    # этого значения, оценка отбрасывается (None) вместо показа координаты.
    # Нужен из-за реальной численной особенности: у угла от надира, близкого
    # к 90° (почти горизонт), tan() резко растёт -- небольшая неточность
    # телеметрии/FOV даёт результат в ДЕСЯТКИ КИЛОМЕТРОВ, формально "меньше
    # 90°", но физически бессмысленно. Нашли на реальных данных: pitch~-9°,
    # дало 69.8 км вместо разумной оценки. Лучше показать "нет оценки", чем
    # координату, которая уведёт поиск на 70 км в сторону.
    "max_estimate_distance_m": sar_common.DEFAULT_MAX_ESTIMATE_DISTANCE_M,
    "grouping": {
        # максимальный разрыв между кадрами (в единицах frame_idx, т.е.
        # с учётом frame_skip), при котором детекции ещё считаются одной
        # и той же сценой
        "max_frame_gap": 360,
        # насколько может "уехать" бокс между двумя кадрами одной сцены,
        # в множителях от диагонали бокса (бокс двигается — объект в кадре
        # не стоит на месте пиксель в пиксель)
        "distance_multiplier": 2.5,
    },
}


def load_config(config_path):
    """Загружает конфиг, сливая его поверх DEFAULT_CONFIG (глубоко для grouping)."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if config_path and os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        for k, v in user_cfg.items():
            if k == "grouping" and isinstance(v, dict):
                cfg["grouping"].update(v)
            else:
                cfg[k] = v
    return cfg


# ---------------------------------------------------------------------------
# 1. ИНФЕРЕНС — единственное место, которое нужно менять под свою модель
# ---------------------------------------------------------------------------

_MODEL = None
_MODEL_TYPE = "custom"


# Классы COCO в порядке индексов модели. Держим списком здесь, а не тянем
# из пакета модели: это обычные данные, и они не тащат за собой лицензию.
# YOLOX-s, обученная на COCO. Apache-2.0 -- в отличие от ultralytics/YOLOv8
# (AGPL-3.0), это позволяет держать проект под MIT и передавать его кому
# угодно, в том числе в закрытом виде.
YOLOX_ONNX_URL = ("https://github.com/Megvii-BaseDetection/YOLOX/releases/"
                  "download/0.1.1rc0/yolox_s.onnx")

COCO_CLASSES = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat",
    "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack",
    "umbrella", "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball",
    "kite", "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket",
    "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
    "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
    "couch", "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
    "toothbrush",
)

_MODEL_INPUT_SIZE = None   # (h, w), берётся из самой модели
_MODEL_INPUT_NAME = None


def load_model(weights_path: str, model_type: str, target_classes):
    """Загрузка модели через ONNX Runtime.

    Почему не ultralytics: их пакет и веса под AGPL-3.0, а это обязывает
    открывать под AGPL всё, что с ними связано, и мешает передать проект
    кому-либо в закрытом виде. ONNX Runtime -- MIT, веса YOLOX -- Apache-2.0,
    поэтому проект остаётся честно MIT и его можно дарить без оговорок.

    model_type сохранён для совместимости конфигов, но на загрузку больше не
    влияет: классы берутся из модели, отбор нужных -- в run_inference_on_tile.
    """
    global _MODEL, _MODEL_TYPE, _MODEL_INPUT_SIZE, _MODEL_INPUT_NAME
    import onnxruntime as ort

    if not os.path.exists(weights_path):
        raise FileNotFoundError(
            f"Не найден файл модели: {weights_path}\n"
            f"Скачайте YOLOX-s (Apache-2.0):\n"
            f"  {YOLOX_ONNX_URL}\n"
            f"и положите рядом с проектом либо укажите путь в sar_config.json -> model")

    # число потоков ограничено осознанно -- см. _limit_cpu_threads: на той же
    # машине работает веб-сервис, и инференс не должен забирать все ядра
    opts = ort.SessionOptions()
    threads = int(os.environ.get("OMP_NUM_THREADS", "0") or 0)
    if threads > 0:
        opts.intra_op_num_threads = threads
    _MODEL = ort.InferenceSession(weights_path, sess_options=opts,
                                   providers=["CPUExecutionProvider"])
    _MODEL_TYPE = model_type
    inp = _MODEL.get_inputs()[0]
    _MODEL_INPUT_NAME = inp.name
    # форма обычно (1, 3, H, W); если размер динамический -- берём 640
    shape = inp.shape
    h = shape[2] if isinstance(shape[2], int) else 640
    w = shape[3] if isinstance(shape[3], int) else 640
    _MODEL_INPUT_SIZE = (h, w)
    return _MODEL


def _preprocess_yolox(img_bgr, input_size):
    """Готовит тайл для YOLOX: вписывает с сохранением пропорций, добивает
    серым (114) до нужного размера, переставляет в CHW.

    ВАЖНО: YOLOX ждёт BGR 0..255 без нормализации -- ни деления на 255, ни
    перестановки каналов в RGB. Это отличается от большинства моделей, и
    ошибка здесь не падает, а тихо роняет качество детекции."""
    padded = np.ones((input_size[0], input_size[1], 3), dtype=np.uint8) * 114
    r = min(input_size[0] / img_bgr.shape[0], input_size[1] / img_bgr.shape[1])
    resized = cv2.resize(
        img_bgr, (int(img_bgr.shape[1] * r), int(img_bgr.shape[0] * r)),
        interpolation=cv2.INTER_LINEAR).astype(np.uint8)
    padded[: resized.shape[0], : resized.shape[1]] = resized
    chw = np.ascontiguousarray(padded.transpose(2, 0, 1), dtype=np.float32)
    return chw, r


def _decode_yolox(outputs, input_size):
    """Разворачивает сырой выход YOLOX в боксы xyxy и уверенности.

    Сеть предсказывает смещения относительно ячеек трёх сеток (шаги 8/16/32),
    поэтому центр нужно сложить с координатой ячейки и умножить на шаг, а
    ширину/высоту -- прогнать через exp. Без этого координаты будут в
    единицах ячеек, а не пикселей."""
    grids, strides = [], []
    for stride in (8, 16, 32):
        hsize, wsize = input_size[0] // stride, input_size[1] // stride
        xv, yv = np.meshgrid(np.arange(wsize), np.arange(hsize))
        grid = np.stack((xv, yv), 2).reshape(1, -1, 2)
        grids.append(grid)
        strides.append(np.full((1, grid.shape[1], 1), stride))
    grids = np.concatenate(grids, 1)
    strides = np.concatenate(strides, 1)

    outputs = outputs.copy()
    outputs[..., :2] = (outputs[..., :2] + grids) * strides
    outputs[..., 2:4] = np.exp(outputs[..., 2:4]) * strides
    return outputs


def _nms(boxes, scores, iou_threshold=0.45):
    """Обычный NMS. Свой, а не из torchvision -- чтобы не тянуть torch ради
    десяти строк (установка проекта из-за него весила ~2 ГБ)."""
    if len(boxes) == 0:
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0.0, xx2 - xx1 + 1) * np.maximum(0.0, yy2 - yy1 + 1)
        iou = inter / (areas[i] + areas[order[1:]] - inter)
        order = order[1:][iou <= iou_threshold]
    return keep


def run_inference_on_tile(tile_bgr: np.ndarray, conf_threshold: float, target_classes):
    """Вернуть список детекций на ОДНОМ тайле, отфильтрованных по target_classes.

    Возвращает: [(x1, y1, x2, y2, confidence, class_name), ...] в пиксельных
    координатах относительно самого тайла (привязка к полному кадру — снаружи).

    !!! Эта функция — единственное место для замены на ваш инференс !!!
    Если пишете свою обёртку (TensorRT/кастом) — верните список в таком же
    формате, отфильтровав по target_classes самостоятельно.
    """
    if _MODEL is None:
        raise RuntimeError("Модель не загружена -- сначала load_model()")

    blob, ratio = _preprocess_yolox(tile_bgr, _MODEL_INPUT_SIZE)
    raw = _MODEL.run(None, {_MODEL_INPUT_NAME: blob[None, :, :, :]})[0]
    preds = _decode_yolox(raw, _MODEL_INPUT_SIZE)[0]

    # формат строки: [cx, cy, w, h, objectness, ...вероятности классов]
    boxes_xywh = preds[:, :4]
    obj_conf = preds[:, 4:5]
    cls_conf = preds[:, 5:]
    scores_all = obj_conf * cls_conf

    boxes = np.empty_like(boxes_xywh)
    boxes[:, 0] = boxes_xywh[:, 0] - boxes_xywh[:, 2] / 2.0
    boxes[:, 1] = boxes_xywh[:, 1] - boxes_xywh[:, 3] / 2.0
    boxes[:, 2] = boxes_xywh[:, 0] + boxes_xywh[:, 2] / 2.0
    boxes[:, 3] = boxes_xywh[:, 1] + boxes_xywh[:, 3] / 2.0
    boxes /= ratio  # обратно в координаты исходного тайла

    target_lower = [c.strip().lower() for c in target_classes]
    dets = []
    for cls_idx in range(scores_all.shape[1]):
        cls_name = COCO_CLASSES[cls_idx] if cls_idx < len(COCO_CLASSES) else str(cls_idx)
        cls_name_l = cls_name.lower()
        # подстрочное совпадение: "person" матчит "person", "человек" и т.д.
        if not any(t in cls_name_l or cls_name_l in t for t in target_lower):
            continue
        scores = scores_all[:, cls_idx]
        mask = scores > conf_threshold
        if not mask.any():
            continue
        cls_boxes, cls_scores = boxes[mask], scores[mask]
        for i in _nms(cls_boxes, cls_scores):
            x1, y1, x2, y2 = cls_boxes[i]
            dets.append((float(x1), float(y1), float(x2), float(y2),
                          float(cls_scores[i]), cls_name))
    return dets


# ---------------------------------------------------------------------------
# 1b. ДЕТЕКТОР ЦВЕТОВЫХ АНОМАЛИЙ — без модели, работает уже сегодня.
#     Ищет яркие "неприродные" пятна (снаряжение/одежда/палатки) на фоне
#     скал/снега/травы по HSV. Хорошо ловит то, что object detection на
#     мелких объектах часто пропускает.
# ---------------------------------------------------------------------------

# (H_min, H_max, S_min, V_min) диапазоны в OpenCV HSV (H: 0-179)
# Синий/голубой УБРАН намеренно. На горной съёмке это не сигнал, а фон: небо
# и синеватые тени в снегу давали ~167 срабатываний на кадр -- 503 499 штук
# на одно видео против 12 611 у модели. Отчёт раздувался до 296 МБ и
# переставал открываться, а для поиска эти пятна были бесполезны.
# Если когда-нибудь понадобится искать именно синее снаряжение на НЕснежном
# фоне -- вернуть строкой ниже, но осознанно и с проверкой на своём материале:
#     "синий/голубой (тенты, куртки)": [(95, 130, 90, 90)],
COLOR_RANGES = {
    "оранжевый/красный (снаряжение)": [(0, 10, 120, 90), (170, 179, 120, 90)],
    "жёлтый": [(20, 35, 110, 110)],
}


def detect_color_anomalies(frame_bgr, min_area_px=25, max_area_frac=0.02, max_per_frame=12):
    """Возвращает [(x1,y1,x2,y2,score,label), ...] для пятен нетипичного цвета.
    score — эвристическая "уверенность" на основе насыщенности и компактности пятна.

    max_per_frame -- ЖЁСТКИЙ потолок на кадр, оставляются самые уверенные.
    Без него на горном материале детектор вырождался в шум: на реальном
    видео он выдал 503 499 срабатываний на "синий/голубой" (небо и синеватые
    тени в снегу) против 12 611 у модели -- ~167 ложных пятен на КАЖДЫЙ кадр.
    Это не только бесполезно для поиска, но и ломало сервис: report.html
    разрастался до 296 МБ, и страница сцен переставала открываться.
    Настоящая находка почти всегда среди самых ярких/компактных пятен, а
    хвост из сотен слабых -- это фон."""
    h, w = frame_bgr.shape[:2]
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    max_area = max_area_frac * h * w

    results = []
    for label, ranges in COLOR_RANGES.items():
        mask = np.zeros((h, w), dtype=np.uint8)
        for (h_min, h_max, s_min, v_min) in ranges:
            lower = np.array([h_min, s_min, v_min])
            upper = np.array([h_max, 255, 255])
            mask |= cv2.inRange(hsv, lower, upper)

        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in contours:
            area = cv2.contourArea(c)
            if area < min_area_px or area > max_area:
                continue
            x, y, bw, bh = cv2.boundingRect(c)
            region_sat = hsv[y:y + bh, x:x + bw, 1].mean() / 255.0
            compactness = area / (bw * bh + 1e-6)
            score = float(min(1.0, 0.5 * region_sat + 0.5 * compactness))
            results.append((x, y, x + bw, y + bh, score, f"цвет: {label}"))

    if max_per_frame and len(results) > max_per_frame:
        results.sort(key=lambda r: r[4], reverse=True)
        results = results[:max_per_frame]
    return results


# ---------------------------------------------------------------------------
# 2. ТАЙЛИНГ + NMS — режем кадр на перекрывающиеся куски, чтобы модель
#    видела мелкие/дальние объекты, потом сливаем детекции обратно
# ---------------------------------------------------------------------------

def make_tiles(frame_w, frame_h, tile_size, overlap):
    step = int(tile_size * (1 - overlap))
    xs = list(range(0, max(frame_w - tile_size, 0) + 1, step)) or [0]
    ys = list(range(0, max(frame_h - tile_size, 0) + 1, step)) or [0]
    if xs[-1] + tile_size < frame_w:
        xs.append(frame_w - tile_size)
    if ys[-1] + tile_size < frame_h:
        ys.append(frame_h - tile_size)
    tiles = []
    for y in ys:
        for x in xs:
            x = max(0, min(x, frame_w - tile_size))
            y = max(0, min(y, frame_h - tile_size))
            tiles.append((x, y))
    return tiles


def nms(boxes, scores, iou_threshold=0.4):
    if len(boxes) == 0:
        return []
    boxes = np.array(boxes, dtype=np.float32)
    scores = np.array(scores, dtype=np.float32)
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(int(i))
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        w = np.maximum(0.0, xx2 - xx1)
        h = np.maximum(0.0, yy2 - yy1)
        inter = w * h
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
        order = order[1:][iou <= iou_threshold]
    return keep


def detect_frame_tiled(frame_bgr, conf_threshold, tile_size, overlap, target_classes):
    h, w = frame_bgr.shape[:2]
    ts = min(tile_size, h, w)
    tiles = make_tiles(w, h, ts, overlap)

    by_class = {}  # class_name -> (boxes, scores)
    for (tx, ty) in tiles:
        crop = frame_bgr[ty:ty + ts, tx:tx + ts]
        dets = run_inference_on_tile(crop, conf_threshold, target_classes)
        for (x1, y1, x2, y2, conf, cls_name) in dets:
            b, s = by_class.setdefault(cls_name, ([], []))
            b.append((x1 + tx, y1 + ty, x2 + tx, y2 + ty))
            s.append(conf)

    out = []
    for cls_name, (boxes, scores) in by_class.items():
        keep = nms(boxes, scores)  # NMS отдельно по каждому классу
        for i in keep:
            out.append((*boxes[i], scores[i], cls_name))
    return out


# ---------------------------------------------------------------------------
# 3. ТЕЛЕМЕТРИЯ (SRT) — GPS/высота по таймкоду, если файл есть рядом с видео
# ---------------------------------------------------------------------------

_SRT_BLOCK_INDEX_RE = re.compile(r"^\d+$")
_SRT_TIME_RANGE_RE = re.compile(r"-->")


def _clean_raw_telemetry_block(block):
    """Убирает чисто служебный "мусор" SRT-блока (номер блока субтитров,
    строку диапазона таймкодов -- она уже есть отдельно в остальном UI,
    HTML-тег <font>...</font>), оставляя ВСЮ содержательную часть как есть:
    FrameCnt/DiffTime, абсолютное время съёмки, все квадратные скобки с
    метриками (iso/shutter/fnum/ev/focal_len/GPS/высота/подвес и что угодно
    ещё, что пишет конкретная прошивка). Это сырой текст для показа
    человеку "всё, что было в телеметрии на этом кадре" -- специально не
    парсится в поля по отдельности, чтобы ничего не потерять, даже то, что
    эта программа не умеет распознавать по имени."""
    kept = []
    for line in block.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if _SRT_BLOCK_INDEX_RE.match(stripped) or _SRT_TIME_RANGE_RE.search(stripped):
            continue
        cleaned = re.sub(r"</?font[^>]*>", "", stripped, flags=re.I).strip()
        if cleaned:
            kept.append(cleaned)
    return "\n".join(kept)


def parse_srt_telemetry(srt_path, gps_tuple_order="lat_lon"):
    """Парсит DJI-подобный SRT с GPS. Возвращает список (start_seconds,
    end_seconds, {"lat":.., "lon":.., "alt":.., "alt_abs":.., "yaw":..,
    "pitch":.., "roll":.., "focal_len":.., "raw_telemetry":..}) — все ключи
    присутствуют всегда, значение None, если поле не нашлось в конкретном
    блоке субтитров (не каждая прошивка пишет вообще всё).

    "raw_telemetry" -- это ВЕСЬ текст блока (см. _clean_raw_telemetry_block),
    не только то, что распознано в отдельные поля выше. Нужен, чтобы
    показывать человеку полную оригинальную телеметрию кадра "как есть" --
    включая поля, которые эта программа не разбирает по отдельности (iso,
    shutter, fnum, ev, color_md, ae_meter_md, dzoom_ratio и т.п.).

    Поддерживает два формата записи GPS в субтитрах:
      1. С явными подписями: "lat: 42.5 lon: 74.5" — однозначно, без вопросов.
      2. Формат-кортеж без подписей: "GPS(42.5, 74.5, 1500)" — частый у DJI,
         но порядок координат внутри скобок НЕ стандартизирован (разные
         прошивки/приложения пишут то (lat, lon, alt), то (lon, lat, alt)).
         Какой порядок использовать — задаётся gps_tuple_order:
         "lat_lon" (по умолчанию) или "lon_lat". Если координаты у вас в
         отчётах ложатся не туда (например, широта/долгота явно перепутаны
         местами — сверьте с картой) — поменяйте "srt_gps_tuple_order" в
         sar_config.json на противоположное значение.

    Высота: "alt" — относительная (rel_alt, если он есть в файле; иначе
    первое, что нашлось по общему шаблону "alt..."). "alt_abs" — абсолютная
    MSL (abs_alt), отдельно, если DJI её пишет.

    Угол подвеса (gimbal): "yaw"/"pitch"/"roll" — из gb_yaw/gb_pitch/gb_roll,
    если прошивка их пишет. Это ЧИСТО ДОПОЛНИТЕЛЬНЫЕ поля -- отсутствие
    любого из них не влияет на GPS/высоту, ничего не ломает и не требует
    поддержки от старых SRT без этих полей.
    """
    if not srt_path or not os.path.exists(srt_path):
        return []
    try:
        text = open(srt_path, "r", encoding="utf-8", errors="ignore").read()
    except OSError as e:
        # повреждённый/недоступный файл телеметрии не должен ронять анализ
        # видео целиком -- просто работаем дальше без GPS
        print(f"[телеметрия] не удалось прочитать {srt_path}: {e} -- продолжаю без телеметрии")
        return []
    blocks = re.split(r"\n\s*\n", text.strip())
    entries = []
    # ВАЖНО: \D{0,5} здесь НЕНЖАДНЫЙ (?), а не жадный. Жадный \D{0,5} перед
    # числом с необязательным знаком съедал сам минус (он тоже "не цифра")
    # раньше, чем до него добирался [-+]? -- отрицательные значения (южная
    # широта, западная долгота, наклон подвеса вниз -- gb_pitch у дрона,
    # смотрящего на землю, как раз отрицательный) молча превращались в
    # положительные. Нашли на реальных данных: gb_pitch: -56.6 в файле
    # DJI_20260812140054_0003_Z.SRT парсился как +56.6 без нежадности.
    time_re = re.compile(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})")
    lat_re = re.compile(r"lat(?:itude)?\D{0,5}?([-+]?\d{1,3}\.\d+)", re.I)
    lon_re = re.compile(r"lon(?:g|gitude)?\D{0,5}?([-+]?\d{1,3}\.\d+)", re.I)
    alt_re = re.compile(r"alt(?:itude)?\D{0,5}?([-+]?\d+\.?\d*)", re.I)
    abs_alt_re = re.compile(r"abs_alt\D{0,5}?([-+]?\d+\.?\d*)", re.I)
    yaw_re = re.compile(r"gb_yaw\D{0,5}?([-+]?\d+\.?\d*)", re.I)
    pitch_re = re.compile(r"gb_pitch\D{0,5}?([-+]?\d+\.?\d*)", re.I)
    roll_re = re.compile(r"gb_roll\D{0,5}?([-+]?\d+\.?\d*)", re.I)
    focal_len_re = re.compile(r"focal_len\D{0,5}?([-+]?\d+\.?\d*)", re.I)
    gps_tuple_re = re.compile(
        r"GPS\s*\(\s*([-+]?\d{1,3}\.\d+)\s*,\s*([-+]?\d{1,3}\.\d+)\s*(?:,\s*([-+]?\d+\.?\d*))?\s*\)", re.I)

    warned_tuple_format = False
    for block in blocks:
        m = time_re.search(block)
        if not m:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = map(int, m.groups())
        start = h1 * 3600 + m1 * 60 + s1 + ms1 / 1000
        end = h2 * 3600 + m2 * 60 + s2 + ms2 / 1000

        lat_m, lon_m, alt_m = lat_re.search(block), lon_re.search(block), alt_re.search(block)
        lat_val = float(lat_m.group(1)) if lat_m else None
        lon_val = float(lon_m.group(1)) if lon_m else None
        alt_val = float(alt_m.group(1)) if alt_m else None

        abs_alt_m = abs_alt_re.search(block)
        alt_abs_val = float(abs_alt_m.group(1)) if abs_alt_m else None

        yaw_m, pitch_m, roll_m = yaw_re.search(block), pitch_re.search(block), roll_re.search(block)
        yaw_val = float(yaw_m.group(1)) if yaw_m else None
        pitch_val = float(pitch_m.group(1)) if pitch_m else None
        roll_val = float(roll_m.group(1)) if roll_m else None

        # focal_len -- НЕ угол, а число из SRT конкретной прошивки (часто
        # эквивалентный фокус в мм при зуме). Сам по себе не используется в
        # расчёте вероятных координат (см. estimate_ground_point в
        # sar_common.py) -- надёжно перевести его в реальный FOV без данных
        # о сенсоре камеры нельзя. Показывается в UI как есть, чтобы человек
        # мог на глаз заметить "кадр снят с зумом -- оценке верить меньше".
        focal_len_m = focal_len_re.search(block)
        focal_len_val = float(focal_len_m.group(1)) if focal_len_m else None

        if lat_val is None or lon_val is None:
            # подписанных lat/lon нет (или только один есть) -- пробуем
            # формат-кортеж "GPS(a, b, c)" без подписей
            m_tuple = gps_tuple_re.search(block)
            if m_tuple:
                a, b = float(m_tuple.group(1)), float(m_tuple.group(2))
                lat_val, lon_val = (a, b) if gps_tuple_order == "lat_lon" else (b, a)
                if alt_val is None and m_tuple.group(3):
                    alt_val = float(m_tuple.group(3))
                if not warned_tuple_format:
                    print(f"[телеметрия] обнаружен формат GPS(x,y,z) без подписей lat/lon в {srt_path}, "
                          f"использую порядок '{gps_tuple_order}' (см. sar_config.json -> "
                          f"srt_gps_tuple_order, если координаты окажутся перепутаны местами)")
                    warned_tuple_format = True

        entries.append((start, end, {"lat": lat_val, "lon": lon_val, "alt": alt_val,
                                      "alt_abs": alt_abs_val, "yaw": yaw_val,
                                      "pitch": pitch_val, "roll": roll_val,
                                      "focal_len": focal_len_val,
                                      "raw_telemetry": _clean_raw_telemetry_block(block)}))
    return entries


def lookup_telemetry(entries, t_seconds):
    for start, end, data in entries:
        if start <= t_seconds <= end:
            return data
    return {"lat": None, "lon": None, "alt": None, "alt_abs": None,
            "yaw": None, "pitch": None, "roll": None, "focal_len": None, "raw_telemetry": None}


# ---------------------------------------------------------------------------
# 4. ОСНОВНОЙ ПАЙПЛАЙН
# ---------------------------------------------------------------------------

@dataclass
class Hit:
    frame_idx: int
    timestamp: str
    seconds: float
    confidence: float
    object_class: str
    source: str  # "model" или "color"
    bbox: list
    lat: float
    lon: float
    alt: float
    image_path: str       # кроп вокруг детекции (превью в галерее)
    full_image_path: str  # полный кадр с отмеченными боксами (открывается по клику)
    group_id: str = ""    # заполняется после группировки в сцены
    alt_abs: float = None  # абсолютная высота MSL (abs_alt из SRT), если есть
    yaw: float = None       # угол подвеса (gimbal), если прошивка его пишет
    pitch: float = None
    roll: float = None
    focal_len: float = None    # как есть из SRT (см. parse_srt_telemetry) -- для UI-подсказки про зум
    frame_width: int = None    # ширина/высота ПОЛНОГО кадра в пикселях -- нужны, чтобы потом (например,
    frame_height: int = None   # в backfill_telemetry.py) пересчитать bbox в доли кадра без повторного чтения видео
    est_lat: float = None       # ОЦЕНКА координат объекта (не дрона!) -- см. sar_common.estimate_ground_point
    est_lon: float = None       # всегда "вероятные координаты" в UI, не "координаты объекта" -- есть погрешность
    est_distance_m: float = None  # горизонтальное расстояние от дрона до оценённой точки, для контекста
    raw_telemetry: str = None   # весь текст SRT-блока на этом кадре как есть (см. parse_srt_telemetry)


def fmt_timestamp(seconds):
    return str(timedelta(seconds=round(seconds)))


def flush_partial_detections(hits, out_dir):
    """Пишет ТЕКУЩИЙ (ещё не полный) список детекций в detections.json —
    вызывается периодически ПРЯМО ВО ВРЕМЯ обработки, а не только один раз
    в конце. Это даёт возможность смотреть в ручном плеере рамки модели,
    которые она уже успела найти, не дожидаясь 100% готовности видео.

    Пишет через временный файл + os.replace() (атомарная замена на уровне
    ОС что на Linux, что на Windows) — чтобы сервер, читающий этот файл
    параллельно в любой произвольный момент, никогда не увидел наполовину
    записанный/битый JSON.
    """
    tmp_path = os.path.join(out_dir, "detections.json.tmp")
    final_path = os.path.join(out_dir, "detections.json")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump([asdict(h) for h in hits], f, ensure_ascii=False)
        os.replace(tmp_path, final_path)
    except OSError as e:
        # не фатально -- это просто "предварительный просмотр", если не
        # получилось разок записать, попробуем на следующем чекпоинте
        print(f"  (не удалось обновить предварительный просмотр детекций: {e})")


# ---------------------------------------------------------------------------
# 4b. ГРУППИРОВКА В СЦЕНЫ — простой онлайн-трекер без нейросети:
#     детекция продолжает существующую сцену, если у неё тот же класс+source,
#     разрыв по frame_idx <= max_frame_gap, и центр бокса не "улетел" дальше
#     distance_multiplier диагоналей бокса от последней точки трека (бокс
#     может плыть — объект и дрон двигаются).
# ---------------------------------------------------------------------------

def group_hits_into_scenes(hits, max_frame_gap=360, distance_multiplier=2.5):
    from collections import defaultdict

    by_key = defaultdict(list)
    for h in hits:
        by_key[(h.object_class, h.source)].append(h)

    all_groups = []
    for key, key_hits in by_key.items():
        key_hits.sort(key=lambda h: h.frame_idx)
        open_tracks = []  # {'last_frame_idx','last_center','last_diag','hits':[]}

        for h in key_hits:
            cx = (h.bbox[0] + h.bbox[2]) / 2.0
            cy = (h.bbox[1] + h.bbox[3]) / 2.0
            diag = ((h.bbox[2] - h.bbox[0]) ** 2 + (h.bbox[3] - h.bbox[1]) ** 2) ** 0.5
            diag = max(diag, 1e-3)

            # чистим треки, которые точно больше никогда не смогут совпасть
            # (дальше по времени разрыв будет только расти)
            open_tracks = [t for t in open_tracks
                           if h.frame_idx - t["last_frame_idx"] <= max_frame_gap]

            best_track, best_dist = None, None
            for t in open_tracks:
                if h.frame_idx - t["last_frame_idx"] > max_frame_gap:
                    continue
                tcx, tcy = t["last_center"]
                dist = ((cx - tcx) ** 2 + (cy - tcy) ** 2) ** 0.5
                avg_diag = (diag + t["last_diag"]) / 2.0
                if dist <= distance_multiplier * avg_diag:
                    if best_dist is None or dist < best_dist:
                        best_dist, best_track = dist, t

            if best_track is not None:
                best_track["hits"].append(h)
                best_track["last_frame_idx"] = h.frame_idx
                best_track["last_center"] = (cx, cy)
                best_track["last_diag"] = diag
            else:
                open_tracks.append({
                    "last_frame_idx": h.frame_idx,
                    "last_center": (cx, cy),
                    "last_diag": diag,
                    "hits": [h],
                })
                all_groups.append(open_tracks[-1])

    # присваиваем id и заполняем group_id у самих Hit
    groups = []
    for i, t in enumerate(all_groups):
        gid = f"g{i:05d}"
        for h in t["hits"]:
            h.group_id = gid
        groups.append({"id": gid, "hits": t["hits"]})
    return groups


def apply_representative_full_frame_mode(groups, out_dir):
    """Оставляет на диске только представительные полные кадры на группу
    (первый / пиковый по уверенности / последний), остальные удаляет и
    перепривязывает остальные детекции группы на ближайший оставшийся файл."""
    frames_dir = os.path.join(out_dir, "frames")

    required = set()
    for g in groups:
        g_hits = g["hits"]  # уже отсортированы по frame_idx (порядок вставки)
        reps = [g_hits[0], max(g_hits, key=lambda h: h.confidence), g_hits[-1]]
        rep_paths = sorted({(h.frame_idx, h.full_image_path) for h in reps if h.full_image_path})
        for _, path in rep_paths:
            required.add(path)
        g["_rep_paths"] = rep_paths

    for g in groups:
        rep_paths = g["_rep_paths"]
        if not rep_paths:
            continue
        for h in g["hits"]:
            if h.full_image_path not in required:
                nearest = min(rep_paths, key=lambda rp: abs(rp[0] - h.frame_idx))
                h.full_image_path = nearest[1]

    if os.path.isdir(frames_dir):
        for fname in os.listdir(frames_dir):
            rel = os.path.relpath(os.path.join(frames_dir, fname), out_dir)
            if rel not in required:
                try:
                    os.remove(os.path.join(frames_dir, fname))
                except OSError:
                    pass


def process_video(video_path, weights_path, model_type, target_classes, conf,
                   tile_size, overlap, frame_skip, out_dir, srt_path=None,
                   crop_margin=60, use_color_anomaly=True, full_frame_quality=88,
                   full_frame_save_mode="all", max_frame_gap=360, distance_multiplier=2.5,
                   tracking_report_id=None, tracking_api_base=None, srt_gps_tuple_order="lat_lon",
                   camera_hfov_deg=84.0, detector_backend="custom",
                   max_estimate_distance_m=sar_common.DEFAULT_MAX_ESTIMATE_DISTANCE_M,
                   fov_cfg=None, color_max_per_frame=12):
    os.makedirs(out_dir, exist_ok=True)
    crops_dir = os.path.join(out_dir, "crops")
    frames_dir = os.path.join(out_dir, "frames")
    os.makedirs(crops_dir, exist_ok=True)
    os.makedirs(frames_dir, exist_ok=True)

    # camera_hfov_deg остаётся резервом на случай, если в SRT нет focal_len
    _fov_cfg = dict(fov_cfg or {})
    _fov_cfg.setdefault("camera_hfov_deg", camera_hfov_deg)

    load_model(weights_path, model_type, target_classes)
    detect_fn = detect_frame_tiled
    telemetry = parse_srt_telemetry(srt_path, gps_tuple_order=srt_gps_tuple_order)

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"Не удалось открыть видео: {video_path}", file=sys.stderr)
        sys.exit(1)

    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    # ЖЁСТКОЕ ОГРАНИЧЕНИЕ: разрыв между кадрами одной сцены не может
    # превышать число кадров в самом видео — иначе значение из конфига
    # (например 360 на коротком ролике из 150 кадров) бессмысленно и может
    # маскировать ошибку конфигурации. total_frames иногда возвращается
    # некорректно (0 или мусор) некоторыми контейнерами/кодеками — в этом
    # случае clamp не применяем, доверяем значению из конфига как есть.
    effective_max_frame_gap = max_frame_gap
    if total_frames > 0 and max_frame_gap > total_frames:
        effective_max_frame_gap = total_frames
        print(f"ВНИМАНИЕ: max_frame_gap={max_frame_gap} больше числа кадров в видео "
              f"({total_frames}) — ограничиваю разрыв до {effective_max_frame_gap}.")

    hits = []
    frame_idx = 0
    processed = 0

    print(f"[{video_path}] fps={fps:.1f} total_frames={total_frames} "
          f"(обрабатываем каждый {frame_skip}-й кадр)")

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        if frame_idx % frame_skip == 0:
            seconds = frame_idx / fps

            # источник 1: object detection по заданным классам (detect_fn --
            # detect_frame_tiled или detect_frame_sahi, см. detector_backend выше)
            dets = [(x1, y1, x2, y2, s, cls, "model") for (x1, y1, x2, y2, s, cls)
                    in detect_fn(frame, conf, tile_size, overlap, target_classes)]

            # источник 2: цветовые аномалии (снаряжение/одежда/палатки), без модели
            if use_color_anomaly:
                dets += [(x1, y1, x2, y2, s, cls, "color") for (x1, y1, x2, y2, s, cls)
                         in detect_color_anomalies(frame, max_per_frame=color_max_per_frame)]

            if dets:
                # полный кадр сохраняем ЧИСТЫМ, без выжженных рамок — рамки
                # теперь рисуются поверх в браузере (SVG-слой), чтобы их можно
                # было опционально скрывать и чтобы они корректно зумились
                # вместе с картинкой. Один файл на кадр (не на детекцию) —
                # экономит место, контекст остальных находок этого кадра
                # передаётся отдельно через frameBoxes в отчёте.
                full_img_name = f"f{frame_idx:07d}_full.jpg"
                full_img_path = os.path.join(frames_dir, full_img_name)
                cv2.imwrite(full_img_path, frame, [cv2.IMWRITE_JPEG_QUALITY, full_frame_quality])
                full_rel_path = os.path.relpath(full_img_path, out_dir)
            else:
                full_rel_path = None

            for (x1, y1, x2, y2, score, cls_name, source) in dets:
                telem = lookup_telemetry(telemetry, seconds)

                # сохраняем не весь кадр, а увеличенный кроп вокруг детекции —
                # спасателю проще быстро глазами оценить, что это
                h, w = frame.shape[:2]
                cx1 = max(0, int(x1) - crop_margin)
                cy1 = max(0, int(y1) - crop_margin)
                cx2 = min(w, int(x2) + crop_margin)
                cy2 = min(h, int(y2) + crop_margin)
                crop = frame[cy1:cy2, cx1:cx2].copy()
                box_color = (0, 0, 255) if source == "model" else (0, 200, 255)
                cv2.rectangle(
                    crop,
                    (int(x1) - cx1, int(y1) - cy1),
                    (int(x2) - cx1, int(y2) - cy1),
                    box_color, 2
                )

                safe_cls = re.sub(r"[^a-zA-Zа-яА-Я0-9]+", "_", cls_name)[:30]
                img_name = f"f{frame_idx:07d}_{safe_cls}_c{score:.2f}.jpg"
                img_path = os.path.join(crops_dir, img_name)
                cv2.imwrite(img_path, crop, [cv2.IMWRITE_JPEG_QUALITY, 90])

                # ОЦЕНКА координат объекта (не дрона) -- см. предупреждения в
                # sar_common.estimate_ground_point. bbox уже в пиксельных
                # координатах ПОЛНОГО кадра (тайлинг развёрнут раньше), делим
                # центр бокса на размер кадра, чтобы получить доли 0..1.
                bbox_cx_frac = ((x1 + x2) / 2.0) / w
                bbox_cy_frac = ((y1 + y2) / 2.0) / h
                # FOV берём по ФАКТИЧЕСКОМУ зуму этого кадра (focal_len из SRT),
                # а не фиксированный на всё видео -- см. sar_common.resolve_frame_fov
                frame_hfov, frame_vfov = sar_common.resolve_frame_fov(
                    telem.get("focal_len"), _fov_cfg)
                estimate = sar_common.estimate_ground_point(
                    drone_lat=telem["lat"], drone_lon=telem["lon"], altitude_m=telem["alt"],
                    gimbal_yaw_deg=telem.get("yaw"), gimbal_pitch_deg=telem.get("pitch"),
                    bbox_center_frac_x=bbox_cx_frac, bbox_center_frac_y=bbox_cy_frac,
                    horizontal_fov_deg=frame_hfov, vertical_fov_deg=frame_vfov,
                    max_distance_m=max_estimate_distance_m)
                est_lat, est_lon, est_distance_m = estimate if estimate else (None, None, None)

                hits.append(Hit(
                    frame_idx=frame_idx,
                    timestamp=fmt_timestamp(seconds),
                    seconds=round(seconds, 2),
                    confidence=round(score, 3),
                    object_class=cls_name,
                    source=source,
                    bbox=[round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
                    lat=telem["lat"], lon=telem["lon"], alt=telem["alt"],
                    alt_abs=telem.get("alt_abs"), yaw=telem.get("yaw"),
                    pitch=telem.get("pitch"), roll=telem.get("roll"),
                    focal_len=telem.get("focal_len"), frame_width=w, frame_height=h,
                    est_lat=est_lat, est_lon=est_lon, est_distance_m=est_distance_m,
                    raw_telemetry=telem.get("raw_telemetry"),
                    image_path=os.path.relpath(img_path, out_dir),
                    full_image_path=full_rel_path,
                ))

            processed += 1
            if processed % 50 == 0:
                pct = 100 * frame_idx / max(total_frames, 1)
                print(f"  ...{pct:.0f}% ({len(hits)} кандидатов найдено пока что)")
                flush_partial_detections(hits, out_dir)

        frame_idx += 1

    cap.release()

    hits.sort(key=lambda h: h.confidence, reverse=True)

    groups = group_hits_into_scenes(hits, max_frame_gap=effective_max_frame_gap,
                                     distance_multiplier=distance_multiplier)
    print(f"Сгруппировано в {len(groups)} сцен(ы) из {len(hits)} детекций.")

    if full_frame_save_mode == "representative":
        apply_representative_full_frame_mode(groups, out_dir)

    save_outputs(hits, groups, out_dir, video_path,
                 tracking_report_id=tracking_report_id, tracking_api_base=tracking_api_base)
    print(f"\nГотово. Найдено кандидатов: {len(hits)}")
    print(f"Отчёт: {os.path.join(out_dir, 'report.html')}")
    return hits


MAX_HITS_PER_SCENE_IN_REPORT = 80


def _limit_scene_hits(g_hits, limit=MAX_HITS_PER_SCENE_IN_REPORT):
    """Сколько кадров сцены встраивать в report.html.

    Страховка от неоткрываемого отчёта: на реальном горном видео цветовой
    детектор дал 516 110 детекций, и report.html вырос до 296 МБ -- браузер
    такую страницу фактически не открывал. Сами детекции при этом полностью
    сохраняются в detections.json/csv, урезается ТОЛЬКО то, что встраивается
    в HTML для листания кадров сцены.

    Берём равномерно по всей сцене (а не первые N), чтобы сохранить обзор
    от начала до конца, и обязательно оставляем пиковый по уверенности кадр --
    именно он показан на карточке сцены."""
    if len(g_hits) <= limit:
        return g_hits
    peak = max(g_hits, key=lambda h: h.confidence)
    step = len(g_hits) / float(limit)
    picked = [g_hits[int(i * step)] for i in range(limit)]
    if peak not in picked:
        picked[len(picked) // 2] = peak
    # порядок по кадрам -- как и в исходном списке
    picked.sort(key=lambda h: h.frame_idx)
    return picked


def save_outputs(hits, groups, out_dir, video_path, tracking_report_id=None, tracking_api_base=None):
    # JSON + CSV — сырые данные для интеграции/дедупликации между видео,
    # с полем group_id — какой сцене принадлежит каждая детекция
    # encoding="utf-8" здесь ОБЯЗАТЕЛЕН -- без него open() на Windows берёт
    # кодировку локали консоли (cp1251 на этой машине), а detections.json
    # читает обратно sar_server.py с явным encoding="utf-8" -> UnicodeDecodeError
    # на любом кириллическом значении (например, метках цветового детектора
    # "цвет: оранжевый/красный..."). Та же грабля, что и с subprocess-пайпом
    # между воркером и детектором (см. CLAUDE.md), только в файловом I/O.
    with open(os.path.join(out_dir, "detections.json"), "w", encoding="utf-8") as f:
        json.dump([asdict(h) for h in hits], f, ensure_ascii=False, indent=2)

    with open(os.path.join(out_dir, "detections.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["frame_idx", "timestamp", "seconds", "confidence", "object_class",
                    "source", "bbox", "drone_lat", "drone_lon", "alt", "alt_abs", "yaw", "pitch", "roll",
                    "focal_len", "est_object_lat", "est_object_lon", "est_distance_m",
                    "image_path", "full_image_path", "group_id"])
        for h in hits:
            w.writerow([h.frame_idx, h.timestamp, h.seconds, h.confidence, h.object_class,
                        h.source, h.bbox, h.lat, h.lon, h.alt, h.alt_abs, h.yaw, h.pitch, h.roll,
                        h.focal_len, h.est_lat, h.est_lon, h.est_distance_m,
                        h.image_path, h.full_image_path, h.group_id])

    # frameBoxes — все рамки, которые когда-либо были найдены на конкретном
    # полном кадре (across всех сцен/классов), нужно для отрисовки оверлея
    # поверх ЧИСТОГО полного кадра в браузере (см. openFull). Один и тот же
    # full_image_path может встречаться у нескольких hits — например, если
    # на одном кадре видео нашли и человека, и палатку.
    # Потолок на число рамок, рисуемых поверх ОДНОГО кадра. Та же страховка,
    # что и с _limit_scene_hits: при вырождении цветового детектора на кадр
    # приходилось под две сотни рамок -- это и нечитаемо глазом, и раздувало
    # report.html (62 МБ только на эту структуру в реальном случае).
    MAX_BOXES_PER_FRAME = 40
    frame_boxes = {}
    for h in hits:
        if not h.full_image_path:
            continue
        frame_boxes.setdefault(h.full_image_path, [])
        entry = {"bbox": h.bbox, "object_class": h.object_class,
                 "source": h.source, "confidence": h.confidence}
        if entry not in frame_boxes[h.full_image_path]:
            frame_boxes[h.full_image_path].append(entry)
    for path, boxes in frame_boxes.items():
        if len(boxes) > MAX_BOXES_PER_FRAME:
            boxes.sort(key=lambda b: b["confidence"], reverse=True)
            frame_boxes[path] = boxes[:MAX_BOXES_PER_FRAME]

    # --- сборка данных по сценам для отчёта ---
    # аватар сцены = кадр с максимальной уверенностью в группе
    scene_summaries = []
    scene_json = {}
    def _first_not_none(attr):
        return next((getattr(h, attr) for h in g_hits if getattr(h, attr) is not None), None)

    for g in groups:
        g_hits = g["hits"]  # в порядке возрастания frame_idx
        peak = max(g_hits, key=lambda h: h.confidence)
        first, last = g_hits[0], g_hits[-1]
        lat = peak.lat if peak.lat is not None else _first_not_none("lat")
        lon = peak.lon if peak.lon is not None else _first_not_none("lon")
        # "вероятные координаты" объекта -- НЕ дрона (см. sar_common.estimate_ground_point);
        # берём с того же пикового кадра, где взяты и координаты дрона, чтобы пара была согласована
        est_lat = peak.est_lat if peak.est_lat is not None else _first_not_none("est_lat")
        est_lon = peak.est_lon if peak.est_lon is not None else _first_not_none("est_lon")

        scene_summaries.append({
            "id": g["id"],
            "object_class": peak.object_class,
            "source": peak.source,
            "confidence": peak.confidence,
            "avatar_image": peak.image_path,
            "count": len(g_hits),
            "time_range": (f"{first.timestamp}" if first.timestamp == last.timestamp
                           else f"{first.timestamp}–{last.timestamp}"),
            "frame_range": f"{first.frame_idx}-{last.frame_idx}",
            "lat": lat, "lon": lon, "est_lat": est_lat, "est_lon": est_lon,
        })

        scene_json[g["id"]] = {
            "object_class": peak.object_class,
            "time_range": scene_summaries[-1]["time_range"],
            "time_start_sec": first.seconds,
            "time_end_sec": last.seconds,
            "lat": lat, "lon": lon, "est_lat": est_lat, "est_lon": est_lon,
            "hits": [
                {
                    "image_path": h.image_path,
                    "full_image_path": h.full_image_path or h.image_path,
                    "timestamp": h.timestamp,
                    "frame_idx": h.frame_idx,
                    "seconds": h.seconds,
                    "confidence": h.confidence,
                    # полная телеметрия дрона на момент этого конкретного кадра --
                    # координаты ДРОНА, координаты ОЦЕНКИ объекта (не то же самое!),
                    # высота (отн./абс.), углы подвеса, фокусное расстояние (зум)
                    "drone_lat": h.lat, "drone_lon": h.lon,
                    "est_lat": h.est_lat, "est_lon": h.est_lon, "est_distance_m": h.est_distance_m,
                    "alt": h.alt, "alt_abs": h.alt_abs,
                    "yaw": h.yaw, "pitch": h.pitch, "roll": h.roll,
                    "focal_len": h.focal_len,
                    "raw_telemetry": h.raw_telemetry,
                }
                for h in _limit_scene_hits(g_hits)
            ],
        }

    # сортировка карточек сцен по пиковой уверенности — как раньше сортировались отдельные детекции
    scene_summaries.sort(key=lambda s: s["confidence"], reverse=True)

    classes_present = sorted(set(s["object_class"] for s in scene_summaries))
    filter_buttons = "".join(
        f'<label><input type="checkbox" class="clsfilter" value="{c}" checked> {c}</label>'
        for c in classes_present
    )

    cards = []
    for s in scene_summaries:
        src_badge = "МОДЕЛЬ" if s["source"] == "model" else "ЦВЕТ"
        lat_attr = s["lat"] if s["lat"] is not None else ""
        lon_attr = s["lon"] if s["lon"] is not None else ""
        est_lat_attr = s["est_lat"] if s["est_lat"] is not None else ""
        est_lon_attr = s["est_lon"] if s["est_lon"] is not None else ""
        # ссылки на карту -- lat/lon уже провалидированы как float при парсинге
        # SRT/расчёте оценки выше по пайплайну, это не произвольная пользовательская строка
        drone_txt = (
            f'📍 дрон: {s["lat"]:.6f}, {s["lon"]:.6f} '
            f'<a href="https://www.google.com/maps?q={s["lat"]},{s["lon"]}" target="_blank" '
            f'rel="noopener" onclick="event.stopPropagation()">🗺 карта</a>'
            if s["lat"] is not None else "📍 дрон: нет GPS")
        est_txt = (
            f'🎯 вероятные координаты объекта: {s["est_lat"]:.6f}, {s["est_lon"]:.6f} '
            f'<a href="https://www.google.com/maps?q={s["est_lat"]},{s["est_lon"]}" target="_blank" '
            f'rel="noopener" onclick="event.stopPropagation()">🗺 карта</a>'
            if s["est_lat"] is not None else "")
        cards.append(f"""
        <div class="card group-card" data-class="{s['object_class']}" onclick="openScene('{s['id']}')">
          <img src="{s['avatar_image']}" loading="lazy" class="thumb">
          <div class="meta">
            <div class="row1"><span class="conf">{s['confidence']:.0%}</span>
              <span class="badge {s['source']}">{src_badge}</span>
              <span class="cls">{s['object_class']}</span></div>
            <div>{s['time_range']} · {s['count']} кадр(ов)</div>
            <div class="coords-line">{drone_txt}</div>
            {f'<div class="coords-line est">{est_txt}</div>' if est_txt else ''}
            <button class="copybtn" onclick="event.stopPropagation(); copyCoords('{lat_attr}','{lon_attr}', this)">📋 координаты дрона</button>
            {f'<button class="copybtn est" onclick="event.stopPropagation(); copyCoords(\'{est_lat_attr}\',\'{est_lon_attr}\', this)">📋 вероятные координаты</button>' if est_txt else ''}
          </div>
        </div>""")

    scene_json_str = json.dumps(scene_json, ensure_ascii=False)
    frame_boxes_str = json.dumps(frame_boxes, ensure_ascii=False)

    # хук для сервера: если этот отчёт открыт через веб-сервис (sar_server.py),
    # при открытии сцены шлём пинг "эту сцену кто-то посмотрел" — используется
    # для статистики просмотренности на странице списка файлов. Если отчёт
    # открывают просто как локальный файл (без сервера) — переменные пустые,
    # тело функции ничего не делает.
    tracking_script = ""
    player_link_html = ""
    home_link_html = ""
    # PLAYER_BASE -- относительный URL плеера для этого отчёта, чтобы из
    # карточки сцены/полноэкранного кадра можно было провалиться сразу на
    # нужный таймкод (?t=<секунды>) вместо того, чтобы искать его вручную
    # перемоткой. Пусто (JS-строка ''), если отчёт открыт не через сервер --
    # тогда самого /report/.../player/ попросту не существует.
    player_base_js = "''"
    if tracking_report_id and tracking_api_base:
        tracking_script = f"""
function pingSceneViewed(groupId) {{
  const g = sceneData[groupId];
  const name = (document.cookie.match(/(?:^|; )sar_viewer_name=([^;]*)/) || [null, 'аноним'])[1];
  fetch('{tracking_api_base}/api/report/{tracking_report_id}/scene_viewed', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{
      group_id: groupId,
      viewer_name: decodeURIComponent(name),
      time_start_sec: g.time_start_sec,
      time_end_sec: g.time_end_sec,
    }}),
  }}).catch(() => {{}});
}}
"""
        # ссылка на ручной плеер имеет смысл только когда отчёт открыт ЧЕРЕЗ
        # сервер (плеер живёт там же, по тому же report_id) — без сервера
        # переход в никуда, поэтому ссылку не показываем вовсе
        player_link_html = (f'<a href="/report/{tracking_report_id}/player/" '
                             f'style="margin-left:14px;">🎬 ручной плеер (видео + разметка)</a>')
        player_base_js = json.dumps(f"/report/{tracking_report_id}/player/")
        # то же самое для ссылки "к списку файлов" -- "/" существует только
        # когда это отдаёт sar_server.py; при локальном открытии report.html
        # как файла (без сервера) такой ссылки быть не должно вовсе
        home_link_html = '<p><a href="/">&larr; к списку файлов</a></p>'

    html = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>SAR review — {os.path.basename(video_path)}</title>
<style>
body {{ font-family: -apple-system, Arial, sans-serif; background:#111; color:#eee; margin:0; padding:20px;}}
h1 {{ font-size: 18px; }}
.summary {{ color:#aaa; margin-bottom:12px; }}
.filters {{ display:flex; flex-wrap:wrap; gap:12px; margin-bottom:20px; padding:10px;
            background:#1b1b1b; border-radius:8px; border:1px solid #333; }}
.filters label {{ font-size:13px; cursor:pointer; }}
.grid {{ display:grid; grid-template-columns: repeat(auto-fill, minmax(260px,1fr)); gap:14px; }}
.card {{ background:#1b1b1b; border-radius:8px; overflow:hidden; border:1px solid #333; }}
.card.group-card {{ cursor:pointer; transition:transform .1s; }}
.card.group-card:hover {{ transform:translateY(-2px); border-color:#555; }}
.card img.thumb {{ width:100%; display:block; aspect-ratio:4/3; object-fit:cover; }}
.meta {{ padding:10px; font-size:13px; line-height:1.6; }}
.row1 {{ display:flex; align-items:center; gap:8px; margin-bottom:4px; }}
.conf {{ font-weight:bold; color:#ff5555; font-size:16px; }}
.cls {{ color:#8ecbff; font-weight:bold; }}
.badge {{ font-size:10px; padding:2px 6px; border-radius:4px; }}
.badge.model {{ background:#5533aa; }}
.badge.color {{ background:#aa7a00; }}
.copybtn {{ margin-top:6px; margin-right:6px; font-size:11px; padding:4px 8px; border-radius:5px;
            border:1px solid #444; background:#252525; color:#ccc; cursor:pointer; }}
.copybtn:hover {{ background:#333; }}
.copybtn.est {{ border-color:#7a5cff; color:#c3b3ff; }}
.copybtn.est:hover {{ background:#3a2f66; }}
.coords-line {{ font-size:12px; }}
.coords-line.est {{ color:#c3b3ff; }}
.telemetry-box {{ background:#161616; border:1px solid #333; border-radius:6px; padding:10px 12px;
                   margin-bottom:12px; font-size:12px; line-height:1.7; color:#ccc; }}
.telemetry-box .est-line {{ color:#c3b3ff; }}
.telemetry-box .caveat {{ color:#888; font-size:11px; margin-top:6px; }}
.raw-telemetry {{ margin-top:8px; }}
.raw-telemetry summary {{ cursor:pointer; color:#8ecbff; font-size:11px; user-select:none; }}
.raw-telemetry summary:hover {{ color:#b3d9ff; }}
.raw-telemetry pre {{ background:#0d0d0d; border:1px solid #2a2a2a; border-radius:5px; padding:8px 10px;
                       margin:6px 0; font-size:11px; color:#9fef9f; white-space:pre-wrap; word-break:break-word; }}

#modal {{ display:none; position:fixed; inset:0; background:rgba(0,0,0,0.94);
          z-index:1000; padding:20px; box-sizing:border-box; overflow-y:auto; }}
#modal.open {{ display:block; }}
.modal-header {{ display:flex; align-items:center; gap:14px; margin-bottom:16px;
                  position:sticky; top:0; background:rgba(17,17,17,0.97); padding:8px 0; z-index:2; }}
.modal-title {{ flex:1; font-size:14px; color:#ddd; }}
.backbtn, .closebtn {{ cursor:pointer; user-select:none; color:#fff; }}
.backbtn {{ font-size:14px; }}
.closebtn {{ font-size:26px; line-height:1; }}
.scene-grid {{ display:grid; grid-template-columns: repeat(auto-fill, minmax(160px,1fr)); gap:10px; }}
.scene-thumb {{ width:100%; aspect-ratio:4/3; object-fit:cover; border-radius:6px; cursor:zoom-in;
                border:1px solid #333; display:block; }}
.full-image-wrap {{ text-align:center; }}
.full-image {{ max-width:100%; max-height:82vh; object-fit:contain; border-radius:4px; }}
.zoom-viewport {{ position:relative; width:100%; height:82vh; overflow:hidden; border-radius:4px;
                   background:#000; cursor:grab; touch-action:none; }}
.zoom-viewport.dragging {{ cursor:grabbing; }}
.zoom-stage {{ position:absolute; top:50%; left:50%; transform-origin:50% 50%; will-change:transform; }}
.zoom-stage img {{ display:block; max-width:none; user-select:none; -webkit-user-drag:none; }}
.zoom-stage svg {{ position:absolute; top:0; left:0; width:100%; height:100%; pointer-events:none; }}
.box-label {{ font-size:14px; font-weight:bold; paint-order:stroke; stroke:#000; stroke-width:3px; }}
.full-toolbar {{ display:flex; align-items:center; gap:16px; margin-bottom:10px; flex-wrap:wrap; }}
.legend {{ display:flex; align-items:center; gap:14px; font-size:12px; color:#ccc; }}
.legend label {{ display:flex; align-items:center; gap:5px; cursor:pointer; }}
.legend .swatch {{ width:12px; height:12px; border-radius:2px; display:inline-block; }}
.zoom-controls {{ display:flex; gap:6px; margin-left:auto; }}
.zoom-controls button {{ width:28px; height:28px; border-radius:5px; border:1px solid #444;
                          background:#252525; color:#eee; cursor:pointer; font-size:15px; line-height:1; }}
.zoom-controls button:hover {{ background:#333; }}
.zoom-hint {{ font-size:11px; color:#666; }}
</style></head>
<body>
{home_link_html}
<h1>Сцены — {os.path.basename(video_path)}{player_link_html}</h1>
<div class="summary">Всего сцен: {len(scene_summaries)} (из {len(hits)} детекций, сгруппированных по объекту и близости во времени/месте).
Аватар сцены — кадр с максимальной уверенностью. Клик по карточке открывает все кадры сцены.
Фиолетовый бейдж — найдено моделью, жёлтый — по цветовой аномалии.</div>
<div class="filters">{filter_buttons}</div>
<div class="grid" id="grid">
{''.join(cards)}
</div>

<div id="modal">
  <div id="modal-body"></div>
</div>

<script id="scene-data" type="application/json">{scene_json_str}</script>
<script id="frame-boxes-data" type="application/json">{frame_boxes_str}</script>
<script>
const sceneData = JSON.parse(document.getElementById('scene-data').textContent);
const frameBoxes = JSON.parse(document.getElementById('frame-boxes-data').textContent);
const modal = document.getElementById('modal');
const modalBody = document.getElementById('modal-body');
const PLAYER_BASE = {player_base_js};

// ссылка "провалиться в плеер на этом таймкоде" -- пусто, если отчёт открыт
// не через sar_server.py (см. player_base_js в save_outputs)
function playerLinkHtml(seconds) {{
  if (!PLAYER_BASE || seconds === null || seconds === undefined) return '';
  return `<a href="${{PLAYER_BASE}}?t=${{seconds}}" onclick="event.stopPropagation()">🎬 к таймкоду в плеере</a>`;
}}

document.querySelectorAll('.clsfilter').forEach(cb => {{
  cb.addEventListener('change', () => {{
    const active = new Set(Array.from(document.querySelectorAll('.clsfilter:checked')).map(x => x.value));
    document.querySelectorAll('.group-card').forEach(card => {{
      card.style.display = active.has(card.dataset.class) ? '' : 'none';
    }});
  }});
}});

function openScene(groupId) {{
  const g = sceneData[groupId];
  if (typeof pingSceneViewed === 'function') {{ pingSceneViewed(groupId); }}
  modalBody.innerHTML = `
    <div class="modal-header">
      <button class="copybtn" onclick="copyCoords('${{g.lat ?? ''}}','${{g.lon ?? ''}}', this)">📋 координаты</button>
      ${{playerLinkHtml(g.time_start_sec)}}
      <span class="modal-title">${{g.object_class}} · ${{g.hits.length}} кадр(ов) · ${{g.time_range}}</span>
      <span class="closebtn" onclick="closeModal()">&times;</span>
    </div>
    <div class="scene-grid">
      ${{g.hits.map((h, i) => `<img src="${{h.image_path}}" loading="lazy" class="scene-thumb"
            onclick="openFull('${{groupId}}', ${{i}})"
            title="${{h.timestamp}} · ${{(h.confidence*100).toFixed(0)}}%">`).join('')}}
    </div>`;
  modal.classList.add('open');
  modal.scrollTop = 0;
}}

const BOX_COLORS = {{ model: '#ff3b3b', color: '#ffb300' }};
let zoomState = {{ scale: 1, tx: 0, ty: 0, dragging: false, lastX: 0, lastY: 0 }};

function fmtCoord(v) {{ return (v === null || v === undefined) ? null : Number(v).toFixed(6); }}
function fmtNum(v, digits) {{ return (v === null || v === undefined) ? '—' : Number(v).toFixed(digits ?? 1); }}
function mapLink(lat, lon) {{
  return `<a href="https://www.google.com/maps?q=${{lat}},${{lon}}" target="_blank" rel="noopener">🗺 карта</a>`;
}}

function escapeHtml(s) {{
  return String(s).replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
}}

function copyText(text, btn) {{
  const ta = document.createElement('textarea');
  ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.appendChild(ta); ta.select();
  try {{ document.execCommand('copy'); }} catch (e) {{}}
  document.body.removeChild(ta);
  if (navigator.clipboard) {{ navigator.clipboard.writeText(text).catch(() => {{}}); }}
  if (btn) {{ const old = btn.textContent; btn.textContent = 'скопировано ✓'; setTimeout(() => btn.textContent = old, 1200); }}
}}

function renderRawTelemetryDropdown(rawText) {{
  // "вся телеметрия кадра" -- полный сырой текст блока SRT (см.
  // parse_srt_telemetry/_clean_raw_telemetry_block в sar_video_review.py),
  // не только то, что разобрано в отдельные поля выше. Раскрытие
  // автоматически копирует текст в буфер (см. делегированный обработчик
  // 'toggle' ниже), плюс есть отдельная кнопка "копировать" внутри --
  // на случай, если человек закрыл и снова хочет скопировать.
  if (!rawText) return '';
  return `
    <details class="raw-telemetry">
      <summary>📡 Вся телеметрия кадра (раскрыть — скопируется автоматически)</summary>
      <pre>${{escapeHtml(rawText)}}</pre>
      <button class="copybtn raw-copy-btn" type="button">📋 копировать</button>
    </details>`;
}}

// Делегированные обработчики -- вешаются ОДИН РАЗ на document, работают и
// после того, как innerHTML списка/модалки перерисован заново (не нужно
// перепривязывать слушатели на каждый рендер). Событие 'toggle' у <details>
// НЕ всплывает (не bubbles по спецификации DOM) -- поэтому обязательна фаза
// перехвата (последний аргумент true), иначе делегирование на document его
// не поймает.
document.addEventListener('toggle', (e) => {{
  if (e.target.matches && e.target.matches('details.raw-telemetry') && e.target.open) {{
    const pre = e.target.querySelector('pre');
    if (pre) copyText(pre.textContent, null);
  }}
}}, true);
document.addEventListener('click', (e) => {{
  const btn = e.target.closest('.raw-copy-btn');
  if (btn) {{
    const pre = btn.closest('details').querySelector('pre');
    if (pre) copyText(pre.textContent, btn);
  }}
}});

function renderTelemetryBox(h) {{
  const droneLat = fmtCoord(h.drone_lat), droneLon = fmtCoord(h.drone_lon);
  const estLat = fmtCoord(h.est_lat), estLon = fmtCoord(h.est_lon);
  const droneLine = droneLat
    ? `📍 дрон: ${{droneLat}}, ${{droneLon}} ${{mapLink(droneLat, droneLon)}}`
    : '📍 дрон: нет GPS';
  const estLine = estLat
    ? `<div class="est-line">🎯 вероятные координаты объекта: ${{estLat}}, ${{estLon}} `
      + `${{mapLink(estLat, estLon)}} (~${{fmtNum(h.est_distance_m, 0)}} м от дрона)</div>`
    : '';
  return `
    <div class="telemetry-box">
      <div>${{droneLine}}</div>
      ${{estLine}}
      <div>высота: ${{fmtNum(h.alt)}} м (отн.) / ${{fmtNum(h.alt_abs)}} м (абс. MSL)
        · подвес: yaw ${{fmtNum(h.yaw)}}° pitch ${{fmtNum(h.pitch)}}° roll ${{fmtNum(h.roll)}}°
        · фокус: ${{fmtNum(h.focal_len)}}</div>
      ${{estLat ? '<div class="caveat">⚠ "вероятные координаты" -- геометрическая оценка (высота + '
        + 'угол подвеса + положение в кадре), не точное измерение. Зум камеры не учитывается -- '
        + 'смотрите поле "фокус" выше, большое отклонение от дефолта означает, что кадр снят с зумом '
        + 'и точность оценки ниже. В горной местности с перепадом высот погрешность может быть больше.</div>' : ''}}
      ${{renderRawTelemetryDropdown(h.raw_telemetry)}}
    </div>`;
}}

function openFull(groupId, hitIndex) {{
  const g = sceneData[groupId];
  const h = g.hits[hitIndex];
  const boxes = frameBoxes[h.full_image_path] || [];

  const legendItems = [...new Set(boxes.map(b => b.source))];
  const legendHtml = (legendItems.length ? legendItems : ['model', 'color']).map(src => `
    <label><span class="swatch" style="background:${{BOX_COLORS[src]}}"></span>
      ${{src === 'model' ? 'найдено моделью' : 'цветовая аномалия'}}</label>`).join('');

  modalBody.innerHTML = `
    <div class="modal-header">
      <span class="backbtn" onclick="openScene('${{groupId}}')">&larr; назад к сцене</span>
      ${{playerLinkHtml(h.seconds)}}
      <span class="modal-title">${{h.timestamp}} · кадр ${{h.frame_idx}} · ${{(h.confidence*100).toFixed(0)}}%</span>
      <span class="closebtn" onclick="closeModal()">&times;</span>
    </div>
    ${{renderTelemetryBox(h)}}
    <div class="full-toolbar">
      <div class="legend">
        <label><input type="checkbox" id="toggle-boxes" checked onchange="toggleBoxes(this.checked)"> показывать рамки детекций</label>
        ${{legendHtml}}
      </div>
      <span class="zoom-hint">колесо мыши / +− — зум, зажать и тянуть — панорама, двойной клик — сброс</span>
      <div class="zoom-controls">
        <button onclick="zoomBy(1.3)" title="Приблизить">+</button>
        <button onclick="zoomBy(1/1.3)" title="Отдалить">−</button>
        <button onclick="resetZoom()" title="Сбросить">⤾</button>
      </div>
    </div>
    <div class="zoom-viewport" id="zoom-viewport">
      <div class="zoom-stage" id="zoom-stage">
        <img id="full-img" src="${{h.full_image_path}}">
        <svg id="boxes-overlay"></svg>
      </div>
    </div>`;
  modal.scrollTop = 0;

  const img = document.getElementById('full-img');
  img.onload = () => {{
    drawBoxOverlay(img, boxes);
    fitImageToViewport(img);
  }};
  if (img.complete) img.onload();

  setupZoomPan();
}}

function drawBoxOverlay(img, boxes) {{
  const svg = document.getElementById('boxes-overlay');
  const w = img.naturalWidth, h = img.naturalHeight;
  svg.setAttribute('viewBox', `0 0 ${{w}} ${{h}}`);
  svg.innerHTML = boxes.map(b => {{
    const [x1, y1, x2, y2] = b.bbox;
    const color = BOX_COLORS[b.source] || '#ff3b3b';
    const label = `${{b.object_class}} ${{(b.confidence*100).toFixed(0)}}%`;
    const fontSize = Math.max(14, w / 60);
    return `<rect x="${{x1}}" y="${{y1}}" width="${{x2-x1}}" height="${{y2-y1}}"
              fill="none" stroke="${{color}}" stroke-width="${{Math.max(2, w/400)}}"/>
            <text x="${{x1}}" y="${{Math.max(fontSize, y1-6)}}" fill="${{color}}"
              class="box-label" style="font-size:${{fontSize}}px">${{label}}</text>`;
  }}).join('');
}}

function toggleBoxes(visible) {{
  const svg = document.getElementById('boxes-overlay');
  if (svg) svg.style.display = visible ? '' : 'none';
}}

// --- зум/панорама: картинка и SVG-рамки лежат в одном .zoom-stage,
// поэтому при масштабировании контейнера рамки всегда остаются
// пиксель-в-пиксель поверх картинки, независимо от уровня зума ---

function fitImageToViewport(img) {{
  const viewport = document.getElementById('zoom-viewport');
  const vw = viewport.clientWidth, vh = viewport.clientHeight;
  const scale = Math.min(vw / img.naturalWidth, vh / img.naturalHeight, 1);
  zoomState = {{ scale, tx: 0, ty: 0, dragging: false, lastX: 0, lastY: 0 }};
  applyZoomTransform();
}}

function applyZoomTransform() {{
  const stage = document.getElementById('zoom-stage');
  if (!stage) return;
  const img = document.getElementById('full-img');
  stage.style.transform =
    `translate(${{zoomState.tx}}px, ${{zoomState.ty}}px) scale(${{zoomState.scale}})`;
  stage.style.marginLeft = (-img.naturalWidth / 2) + 'px';
  stage.style.marginTop = (-img.naturalHeight / 2) + 'px';
  stage.querySelectorAll('img, svg').forEach(el => {{
    el.style.width = img.naturalWidth + 'px';
    el.style.height = img.naturalHeight + 'px';
  }});
}}

function zoomBy(factor, cx, cy) {{
  const viewport = document.getElementById('zoom-viewport');
  if (!viewport) return;
  const rect = viewport.getBoundingClientRect();
  const px = cx ?? rect.width / 2, py = cy ?? rect.height / 2;
  const newScale = Math.min(20, Math.max(0.1, zoomState.scale * factor));
  const actualFactor = newScale / zoomState.scale;
  // якорим зум на точке курсора: пересчитываем смещение так, чтобы точка
  // под курсором визуально осталась на месте (стейдж зумится от центра
  // вьюпорта — см. transform-origin/top:50%/left:50% в applyZoomTransform)
  const dx = px - rect.width / 2, dy = py - rect.height / 2;
  zoomState.tx = (zoomState.tx - dx) * actualFactor + dx;
  zoomState.ty = (zoomState.ty - dy) * actualFactor + dy;
  zoomState.scale = newScale;
  applyZoomTransform();
}}

function resetZoom() {{
  const img = document.getElementById('full-img');
  if (img) fitImageToViewport(img);
}}

function setupZoomPan() {{
  const viewport = document.getElementById('zoom-viewport');
  if (!viewport) return;

  viewport.addEventListener('wheel', e => {{
    e.preventDefault();
    const rect = viewport.getBoundingClientRect();
    const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
    zoomBy(factor, e.clientX - rect.left, e.clientY - rect.top);
  }}, {{ passive: false }});

  viewport.addEventListener('mousedown', e => {{
    zoomState.dragging = true;
    zoomState.lastX = e.clientX;
    zoomState.lastY = e.clientY;
    viewport.classList.add('dragging');
  }});
  window.addEventListener('mousemove', e => {{
    if (!zoomState.dragging) return;
    zoomState.tx += e.clientX - zoomState.lastX;
    zoomState.ty += e.clientY - zoomState.lastY;
    zoomState.lastX = e.clientX;
    zoomState.lastY = e.clientY;
    applyZoomTransform();
  }});
  window.addEventListener('mouseup', () => {{
    zoomState.dragging = false;
    viewport.classList.remove('dragging');
  }});

  viewport.addEventListener('dblclick', e => {{
    const rect = viewport.getBoundingClientRect();
    if (zoomState.scale > 1.05) {{ resetZoom(); }}
    else {{ zoomBy(2.5, e.clientX - rect.left, e.clientY - rect.top); }}
  }});

  // pinch-zoom тачем (планшеты/сенсорные экраны на месте поиска)
  let pinchStartDist = null, pinchStartScale = 1;
  viewport.addEventListener('touchstart', e => {{
    if (e.touches.length === 2) {{
      pinchStartDist = Math.hypot(
        e.touches[0].clientX - e.touches[1].clientX,
        e.touches[0].clientY - e.touches[1].clientY);
      pinchStartScale = zoomState.scale;
    }} else if (e.touches.length === 1) {{
      zoomState.dragging = true;
      zoomState.lastX = e.touches[0].clientX;
      zoomState.lastY = e.touches[0].clientY;
    }}
  }}, {{ passive: true }});
  viewport.addEventListener('touchmove', e => {{
    if (e.touches.length === 2 && pinchStartDist) {{
      const dist = Math.hypot(
        e.touches[0].clientX - e.touches[1].clientX,
        e.touches[0].clientY - e.touches[1].clientY);
      zoomState.scale = Math.min(20, Math.max(0.1, pinchStartScale * (dist / pinchStartDist)));
      applyZoomTransform();
    }} else if (e.touches.length === 1 && zoomState.dragging) {{
      zoomState.tx += e.touches[0].clientX - zoomState.lastX;
      zoomState.ty += e.touches[0].clientY - zoomState.lastY;
      zoomState.lastX = e.touches[0].clientX;
      zoomState.lastY = e.touches[0].clientY;
      applyZoomTransform();
    }}
  }}, {{ passive: true }});
  viewport.addEventListener('touchend', () => {{ zoomState.dragging = false; pinchStartDist = null; }});
}}

function closeModal() {{ modal.classList.remove('open'); modalBody.innerHTML = ''; }}
document.addEventListener('keydown', e => {{ if (e.key === 'Escape') closeModal(); }});

function copyCoords(lat, lon, btn) {{
  if (!lat || lat === 'None' || lat === '') {{
    alert('GPS-координаты для этой сцены недоступны (нет SRT-телеметрии)');
    return;
  }}
  copyText(`${{lat}}, ${{lon}}`, btn);
}}
{tracking_script}
</script>
</body></html>"""

    with open(os.path.join(out_dir, "report.html"), "w", encoding="utf-8") as f:
        f.write(html)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="SAR: покадровый разбор видео с дрона — люди, палатки, снаряжение")
    p.add_argument("--video", required=True, help="путь к видеофайлу")
    p.add_argument("--out", required=True, help="папка для результатов")
    p.add_argument("--config", default=None,
                   help="путь к JSON-конфигу с дефолтами (если не указан — ищем sar_config.json рядом со скриптом; "
                        "если конфига нет вообще — поведение как раньше, без изменений)")

    # Все флаги ниже по умолчанию None -> если не переданы явно, берутся из конфига.
    p.add_argument("--model", default=None,
                   help="путь/имя весов модели (.pt для custom, или yolov8x-worldv2.pt для yolo-world)")
    p.add_argument("--model-type", choices=["custom", "yolo-world"], default=None,
                   help="custom = ваша обученная модель; yolo-world = открытый словарь без дообучения")
    p.add_argument("--classes", default=None,
                   help='классы через запятую, напр. "person,tent,backpack,helmet,ice axe,trekking pole"')
    p.add_argument("--conf", type=float, default=None,
                   help="порог уверенности (низкий — для максимального recall)")
    p.add_argument("--tile", type=int, default=None, help="размер тайла в пикселях")
    p.add_argument("--overlap", type=float, default=None, help="перекрытие тайлов (0-0.5)")
    p.add_argument("--frame-skip", type=int, default=None,
                   help="обрабатывать каждый N-й кадр (1 = все кадры, медленнее)")
    p.add_argument("--srt", default=None,
                   help="путь к SRT-телеметрии дрона (если не указан — ищем видео.srt рядом)")
    p.add_argument("--no-color-anomaly", action="store_true",
                   help="принудительно отключить детектор цветовых аномалий, независимо от конфига")
    p.add_argument("--crop-margin", type=int, default=None, help="отступ вокруг детекции при сохранении кропа")
    p.add_argument("--full-frame-quality", type=int, default=None, help="качество JPEG для полных кадров (1-100)")
    p.add_argument("--full-frame-save-mode", choices=["all", "representative"], default=None,
                   help="all = как раньше (полный кадр на каждый кадр с детекцией); "
                        "representative = экономия диска, оставляет только 1-3 кадра на сцену")
    p.add_argument("--max-frame-gap", type=int, default=None,
                   help="макс. разрыв между кадрами (в frame_idx), при котором детекции считаются одной сценой")
    p.add_argument("--distance-multiplier", type=float, default=None,
                   help="насколько бокс может 'уехать' между кадрами одной сцены (в диагоналях бокса)")
    p.add_argument("--tracking-report-id", default=None,
                   help="(используется sar_server.py) report_id для встраивания хука статистики просмотров")
    p.add_argument("--tracking-api-base", default=None,
                   help="(используется sar_server.py) базовый URL сервера для хука статистики просмотров")
    p.add_argument("--srt-gps-tuple-order", choices=["lat_lon", "lon_lat"], default=None,
                   help="порядок координат в формате телеметрии GPS(a,b,c) без подписей")
    p.add_argument("--telemetry-dir", default=None,
                   help="папка с SRT-телеметрией (рекурсивно), резервный источник, если рядом с "
                        "видео нет videoname.srt (по умолчанию 'telemetry' рядом с видео)")
    p.add_argument("--camera-hfov-deg", type=float, default=None,
                   help="горизонтальный угол обзора камеры дрона в градусах -- для оценки вероятных "
                        "координат объекта детекции (см. camera_hfov_deg в sar_config.json). "
                        "ОБЯЗАТЕЛЬНО проверьте под вашу камеру, дефолт 84° -- не гарантия")
    p.add_argument("--detector-backend", choices=["custom"], default=None,
                   help="custom = собственный тайлинг/NMS (единственный вариант; бэкенд SAHI "
                        "убран вместе с ultralytics, поверх которого он только и работал)")
    p.add_argument("--max-estimate-distance-m", type=float, default=None,
                   help="потолок в метрах для 'вероятных координат' объекта -- расчёты дальше "
                        "отбрасываются как численный артефакт (см. max_estimate_distance_m в "
                        "sar_config.json), дефолт 3000")
    args = p.parse_args()

    config_path = args.config
    if config_path is None:
        candidate = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sar_config.json")
        if os.path.exists(candidate):
            config_path = candidate
            print(f"Использую конфиг: {config_path}")
    cfg = load_config(config_path)
    # точное число зарезервированных под веб-сервис ядер -- из конфига
    # (базовое ограничение уже стоит с момента импорта, см. _limit_cpu_threads)
    try:
        import torch
        torch.set_num_threads(_limit_cpu_threads(cfg.get("detector_reserve_cores", 2)))
    except Exception:
        pass

    def pick(cli_val, cfg_key):
        return cli_val if cli_val is not None else cfg[cfg_key]

    model = pick(args.model, "model")
    if not model:
        p.error("--model не задан ни в аргументах, ни в конфиге")
    model_type = pick(args.model_type, "model_type")
    classes_str = pick(args.classes, "classes")
    conf = pick(args.conf, "conf")
    tile = pick(args.tile, "tile")
    overlap = pick(args.overlap, "overlap")
    frame_skip = pick(args.frame_skip, "frame_skip")
    crop_margin = pick(args.crop_margin, "crop_margin")
    full_frame_quality = pick(args.full_frame_quality, "full_frame_quality")
    full_frame_save_mode = pick(args.full_frame_save_mode, "full_frame_save_mode")
    max_frame_gap = args.max_frame_gap if args.max_frame_gap is not None else cfg["grouping"]["max_frame_gap"]
    distance_multiplier = (args.distance_multiplier if args.distance_multiplier is not None
                            else cfg["grouping"]["distance_multiplier"])
    use_color_anomaly = cfg["color_anomaly"] and not args.no_color_anomaly

    srt_path = args.srt
    if srt_path is None:
        candidate = os.path.splitext(args.video)[0] + ".srt"
        if os.path.exists(candidate):
            srt_path = candidate
            print(f"Найдена телеметрия рядом с видео: {srt_path}")
        else:
            # резервный источник -- индексированная папка telemetry/ (рекурсивно,
            # см. sar_common.build_telemetry_index). Индекс строится один раз за
            # этот запуск (один запуск = одно видео, sar_worker.py и так запускает
            # отдельный процесс на файл) -- никакого повторного обхода диска.
            telemetry_dir_name = pick(args.telemetry_dir, "telemetry_dir")
            video_dir = os.path.dirname(os.path.abspath(args.video))
            telemetry_dir = sar_common.resolve_telemetry_dir(video_dir, telemetry_dir_name)
            telemetry_index = sar_common.build_telemetry_index(telemetry_dir)
            match, reason = sar_common.find_telemetry_for_video(args.video, telemetry_index)
            if match:
                srt_path = str(match)
                print(f"Найдена телеметрия в {telemetry_dir}: {srt_path} ({reason})")
            else:
                print(f"Телеметрия для {os.path.basename(args.video)} не найдена ни рядом с "
                      f"видео, ни в {telemetry_dir} -- отчёт будет без GPS.")

    target_classes = [c.strip() for c in classes_str.split(",") if c.strip()]

    process_video(
        video_path=args.video,
        weights_path=model,
        model_type=model_type,
        target_classes=target_classes,
        conf=conf,
        tile_size=tile,
        overlap=overlap,
        frame_skip=frame_skip,
        out_dir=args.out,
        srt_path=srt_path,
        crop_margin=crop_margin,
        use_color_anomaly=use_color_anomaly,
        full_frame_quality=full_frame_quality,
        full_frame_save_mode=full_frame_save_mode,
        max_frame_gap=max_frame_gap,
        distance_multiplier=distance_multiplier,
        tracking_report_id=args.tracking_report_id,
        tracking_api_base=args.tracking_api_base,
        srt_gps_tuple_order=pick(args.srt_gps_tuple_order, "srt_gps_tuple_order"),
        camera_hfov_deg=pick(args.camera_hfov_deg, "camera_hfov_deg"),
        detector_backend=pick(args.detector_backend, "detector_backend"),
        max_estimate_distance_m=pick(args.max_estimate_distance_m, "max_estimate_distance_m"),
        fov_cfg=cfg,
        color_max_per_frame=cfg.get("color_max_per_frame", 12),
    )


if __name__ == "__main__":
    main()
