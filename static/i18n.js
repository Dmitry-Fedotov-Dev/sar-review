/* Перевод интерфейса на английский — словарь на клиенте.
 *
 * Зачем клиентом, а не на сервере: шаблонов в sar_server.py четырнадцать,
 * и серверная локализация означала бы хирургию по девяти тысячам строк
 * живого файла. Здесь же не трогается ни один шаблон — скрипт подключается
 * единой точкой в after_request, как и положено по граблям проекта
 * (heartbeat однажды стоял на 3 страницах из 6 именно потому, что его
 * вставляли копированием).
 *
 * ГЛАВНОЕ ПРАВИЛО БЕЗОПАСНОСТИ: заменяется только такой текстовый узел,
 * чьё содержимое ЦЕЛИКОМ совпадает с ключом словаря. Никаких подстрок и
 * никаких регулярных выражений по пользовательскому тексту — иначе имя
 * волонтёра, название файла или комментарий превратились бы в кашу.
 * Из-за этого правила перевод местами неполный, и это осознанный размен:
 * лучше непереведённая строка, чем испорченные данные человека.
 *
 * Файл лежит свой, в static/ — платформа обязана работать в поле без
 * интернета, поэтому никаких внешних сервисов перевода тут нет и быть
 * не может.
 */
(function () {
  "use strict";

  var LANG_KEY = "sar_lang";
  var dict = null;          // ru -> en
  var applied = false;      // включён ли сейчас английский
  var observer = null;
  var pending = 0;

  /* localStorage может бросить или вернуть пусто: приватное окно,
     заблокированные данные сайта, превью. Поэтому обе операции в try. */
  function readLang() {
    try { return localStorage.getItem(LANG_KEY) || "ru"; }
    catch (e) { return "ru"; }
  }
  function writeLang(v) {
    try { localStorage.setItem(LANG_KEY, v); }
    catch (e) { /* не сохранилось — переживём, язык просто не запомнится */ }
  }

  /* Узлы, внутрь которых лезть нельзя: содержимое скриптов и стилей,
     поля ввода (там значение пользователя) и всё, что человек правит. */
  var SKIP_TAGS = { SCRIPT: 1, STYLE: 1, TEXTAREA: 1, CODE: 1, PRE: 1 };

  function translateTextNodes(root) {
    if (!dict) return;
    var walker = document.createTreeWalker(
      root, NodeFilter.SHOW_TEXT,
      {
        acceptNode: function (n) {
          var p = n.parentNode;
          if (!p || SKIP_TAGS[p.nodeName]) return NodeFilter.FILTER_REJECT;
          if (p.isContentEditable) return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_ACCEPT;
        }
      }
    );
    var node, batch = [];
    while ((node = walker.nextNode())) batch.push(node);

    for (var i = 0; i < batch.length; i++) {
      var n = batch[i];
      var raw = n.nodeValue;
      var key = raw.trim();
      if (!key) continue;
      var hit = dict[key];
      if (hit === undefined) continue;
      // сохраняем исходник, чтобы уметь вернуть русский без перезагрузки
      if (n.__sarRu === undefined) n.__sarRu = raw;
      n.nodeValue = raw.replace(key, hit);
    }
  }

  /* Атрибуты переводим списком, а не подряд: value трогать нельзя вовсе —
     это отправляемые данные, а не подпись. */
  var ATTRS = ["placeholder", "title", "aria-label", "alt"];

  function translateAttrs(root) {
    if (!dict) return;
    for (var a = 0; a < ATTRS.length; a++) {
      var name = ATTRS[a];
      var els = root.querySelectorAll ? root.querySelectorAll("[" + name + "]") : [];
      for (var i = 0; i < els.length; i++) {
        var el = els[i];
        var cur = el.getAttribute(name);
        if (!cur) continue;
        var key = cur.trim();
        var hit = dict[key];
        if (hit === undefined) continue;
        var store = "__sarRu_" + name;
        if (el[store] === undefined) el[store] = cur;
        el.setAttribute(name, hit);
      }
    }
  }

  function restore(root) {
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    var node;
    while ((node = walker.nextNode())) {
      if (node.__sarRu !== undefined) {
        node.nodeValue = node.__sarRu;
        delete node.__sarRu;
      }
    }
    for (var a = 0; a < ATTRS.length; a++) {
      var name = ATTRS[a], store = "__sarRu_" + name;
      var els = document.querySelectorAll("[" + name + "]");
      for (var i = 0; i < els.length; i++) {
        if (els[i][store] !== undefined) {
          els[i].setAttribute(name, els[i][store]);
          delete els[i][store];
        }
      }
    }
  }

  function applyAll() {
    translateTextNodes(document.body);
    translateAttrs(document);
    document.documentElement.setAttribute("lang", "en");
  }

  /* Половина интерфейса рисуется скриптом уже после загрузки (карточка
     операции, список сцен, плеер), поэтому одного прохода мало. Следим за
     деревом, но не чаще чем раз в кадр: иначе на списке из двухсот файлов
     наблюдатель начнёт драться сам с собой. */
  function watch() {
    if (observer || !window.MutationObserver) return;
    observer = new MutationObserver(function () {
      if (pending) return;
      pending = requestAnimationFrame(function () {
        pending = 0;
        if (applied) applyAll();
      });
    });
    observer.observe(document.body, { childList: true, subtree: true });
  }

  function setLang(lang) {
    if (lang === "en") {
      if (!dict) { load(function () { setLang("en"); }); return; }
      applied = true;
      applyAll();
      watch();
    } else {
      applied = false;
      restore(document.body);
      document.documentElement.setAttribute("lang", "ru");
    }
    writeLang(lang);
    paintButton();
  }

  function load(cb) {
    if (dict) { cb && cb(); return; }
    fetch("/static/i18n.en.json", { cache: "no-cache" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (j) { dict = j; cb && cb(); })
      .catch(function (e) {
        // Пустой catch в этом проекте запрещён: молчаливый отказ здесь
        // выглядел бы как «кнопка не работает», и никто бы не узнал почему.
        console.error("i18n: словарь не загрузился", e);
        var b = document.getElementById("sar-lang-btn");
        if (b) { b.textContent = "EN ?"; b.title = "Словарь не загрузился, см. консоль"; }
      });
  }

  function paintButton() {
    var b = document.getElementById("sar-lang-btn");
    if (!b) return;
    b.textContent = applied ? "RU" : "EN";
    b.title = applied ? "Вернуть русский интерфейс" : "Switch interface to English";
  }

  function addButton() {
    if (document.getElementById("sar-lang-btn")) return;
    var b = document.createElement("button");
    b.id = "sar-lang-btn";
    b.type = "button";
    b.setAttribute("translate", "no");
    b.style.cssText = [
      "position:fixed", "right:12px", "bottom:12px", "z-index:99999",
      "padding:6px 11px", "border-radius:16px",
      "border:1px solid rgba(127,127,127,.45)",
      "background:rgba(28,32,34,.88)", "color:#e6ebe8",
      "font:600 12px/1 system-ui,sans-serif", "cursor:pointer",
      "letter-spacing:.06em"
    ].join(";");
    b.addEventListener("click", function () {
      setLang(applied ? "ru" : "en");
    });
    document.body.appendChild(b);
    paintButton();
  }

  function init() {
    if (!document.body) return;
    addButton();
    if (readLang() === "en") setLang("en");
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
