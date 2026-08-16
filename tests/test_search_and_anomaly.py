"""Поиск по имени файла, статус «аномалия» и скрытие находок модели."""
import sar_common
import sar_server


# --- статус "аномалия" ---

def test_anomaly_status_exists():
    assert "anomaly" in sar_common.VALID_PRIORITIES
    assert "аномалия" in sar_common.PRIORITY_LABELS["anomaly"].lower()


def test_anomaly_is_offered_in_the_player_dropdown():
    assert "'anomaly'" in sar_server.PLAYER_PAGE_HTML


def test_anomaly_is_not_exported_to_the_training_dataset():
    """Обучать модель на том, что человек сам не смог опознать, нельзя --
    «аномалия» означает "непонятно", а не "вот так выглядит находка"."""
    import sar_dataset_export as export_mod
    assert "anomaly" not in export_mod.PRIORITY_TO_CLASS
    assert "anomaly" not in export_mod.EXPORTED_PRIORITIES


def test_anomaly_can_be_saved_through_the_api(tmp_path, monkeypatch):
    import sqlite3
    db_path = str(tmp_path / "sar_data.db")
    sar_common.init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO reports (report_id, rel_path, abs_path, kind, status, out_dir, "
        "file_ctime, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("rep1", "v.mp4", "/w/v.mp4", "video", "done", "/out", 0.0,
         "2026-01-01T00:00:00", "2026-01-01T00:00:00"))
    conn.commit()
    conn.close()

    monkeypatch.setattr(sar_server, "DB_PATH", db_path, raising=False)
    sar_server.app.secret_key = "t"
    sar_server.app.testing = True
    client = sar_server.app.test_client()
    with client.session_transaction() as s:
        s["authed"] = True
        s["viewer_name"] = "tester"

    resp = client.post("/api/report/rep1/priorities",
                        json={"kind": "manual", "ref_key": "1", "priority": "anomaly"})
    assert resp.status_code == 200
    assert resp.get_json()["priority"] == "anomaly"


# --- поиск ---

def test_search_input_exists_on_the_file_list():
    html = sar_server.TREE_PAGE_HTML
    assert 'id="search"' in html
    assert "matchesSearch" in html


def test_search_is_applied_on_every_render():
    """Список сам обновляется каждые 5 секунд -- если фильтр применять
    разово, набранный запрос сбрасывался бы при первом автообновлении."""
    html = sar_server.TREE_PAGE_HTML.replace("\n", " ")
    render = html[html.index("function render(data)"):]
    assert ".filter(matchesSearch)" in render[:900]


def test_search_matches_words_in_any_order():
    html = sar_server.TREE_PAGE_HTML.replace("\n", " ")
    assert "searchQuery.split" in html
    assert ".every(" in html


def test_search_shows_a_message_when_nothing_found():
    assert "ничего не найдено" in sar_server.TREE_PAGE_HTML


def test_search_query_is_escaped_in_the_empty_message():
    """Запрос печатает пользователь -- в HTML он должен попадать
    экранированным, иначе это XSS через строку поиска."""
    html = sar_server.TREE_PAGE_HTML
    assert "escapeHtmlTree(searchQuery)" in html


# --- скрытие находок модели ---

def test_one_checkbox_hides_both_boxes_and_scene_list():
    html = sar_server.PLAYER_PAGE_HTML
    assert 'id="ai-scenes-block"' in html
    assert "applyAiVisibility" in html
    # блок сцен прячется вместе с рамками, по той же галке
    assert "block.style.display = showAiBoxes" in html.replace("\n", " ")
