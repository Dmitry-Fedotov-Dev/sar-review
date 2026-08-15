"""Тесты для ограничения размера in-memory кэшей sar_server.py
(_ai_detections_cache/_telemetry_cache) -- раньше росли без верхней границы
за всё время жизни процесса."""
import sar_server


def test_cache_put_evicts_oldest_when_over_limit(monkeypatch):
    monkeypatch.setattr(sar_server, "_CACHE_MAX_REPORTS", 3)
    cache = {}
    sar_server._cache_put(cache, "a", 1)
    sar_server._cache_put(cache, "b", 2)
    sar_server._cache_put(cache, "c", 3)
    assert list(cache) == ["a", "b", "c"]

    sar_server._cache_put(cache, "d", 4)  # превышаем лимит -- вытесняем самый старый ("a")
    assert list(cache) == ["b", "c", "d"]
    assert "a" not in cache


def test_cache_put_updating_existing_key_does_not_evict(monkeypatch):
    monkeypatch.setattr(sar_server, "_CACHE_MAX_REPORTS", 2)
    cache = {}
    sar_server._cache_put(cache, "a", 1)
    sar_server._cache_put(cache, "b", 2)
    sar_server._cache_put(cache, "a", 100)  # обновление существующего ключа, не новая вставка
    assert cache == {"a": 100, "b": 2}
    assert list(cache) == ["a", "b"]
