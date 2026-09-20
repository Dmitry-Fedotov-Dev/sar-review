"""Проверка облачного подключения ДО того, как трогать платформу.

ЗАЧЕМ ОТДЕЛЬНЫЙ СКРИПТ. Когда «не работает», причин может быть три:
токен не тот, папка не та, платформа не так настроена. Разбираться во всех
трёх сразу -- худший способ. Этот скрипт проверяет ПЕРВЫЕ ДВЕ, ничего не
трогая: он не пишет в базу, не качает файлы и не меняет настройки.

Что показывает:
  * принимает ли хранилище токен;
  * что лежит в указанной папке и сколько это весит;
  * сколько из найденного платформа сочтёт материалом;
  * сойдётся ли это с текущими ограничениями (потолок временной папки).

Использование:
    python check_cloud.py --provider yandex --token ТОКЕН
    python check_cloud.py --provider google --token ТОКЕН --folder ID_ПАПКИ
    python check_cloud.py --provider yandex --token ТОКЕН --folder "disk:/Курумды"

    # скачать один самый маленький файл и убедиться, что байты доходят
    python check_cloud.py --provider yandex --token ТОКЕН --download-smallest

ТОКЕН НЕ СОХРАНЯЕТСЯ. Он живёт только в аргументах этого запуска.
"""
import argparse
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sar_cloud
import sar_common


def human(n):
    if n >= 1e9:
        return "%.2f ГБ" % (n / 1e9)
    if n >= 1e6:
        return "%.0f МБ" % (n / 1e6)
    return "%.0f КБ" % (n / 1e3)


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--provider", required=True, choices=sorted(sar_cloud.PROVIDERS))
    p.add_argument("--token", required=True)
    p.add_argument("--folder", default=None,
                   help="google: идентификатор папки (по умолчанию root); "
                        "yandex: путь вида disk:/Папка (по умолчанию disk:/)")
    p.add_argument("--depth", type=int, default=2,
                   help="на сколько уровней вглубь смотреть (по умолчанию 2)")
    p.add_argument("--download-smallest", action="store_true",
                   help="скачать самый маленький файл во временную папку "
                        "системы и удалить -- проверка, что байты доходят")
    args = p.parse_args()

    folder = args.folder or ("disk:/" if args.provider == "yandex" else "root")
    try:
        prov = sar_cloud.make_provider(args.provider, args.token)
    except sar_cloud.CloudError as e:
        # Сам токен не похож на токен -- до сети дело не дойдёт. Говорим об
        # этом сразу и человеческим языком: трассировка в момент настройки
        # не помогает никому.
        print("ТОКЕН НЕ ПРИНЯТ: %s" % e)
        return 1

    print("Хранилище: %s" % prov.label)
    print("Папка:     %s" % folder)
    print()

    # --- 1. Принимают ли токен ---------------------------------------------
    print("1. Проверяю токен и доступ к папке...")
    t0 = time.time()
    try:
        items = prov.list_folder(folder)
    except sar_cloud.AuthExpired as e:
        print("   ОТКАЗ: %s" % e)
        print()
        print("   Токен не принят. Частые причины:")
        print("     * у Google токен живёт ОДИН ЧАС -- выпустите заново;")
        print("     * токен выдан без права читать файлы;")
        print("     * скопирован не тот токен (нужен access token).")
        return 2
    except sar_cloud.RateLimited as e:
        print("   ОТКАЗ: %s" % e)
        print("   Хранилище просит подождать. Повторите через несколько минут.")
        return 3
    except sar_cloud.CloudError as e:
        print("   ОТКАЗ: %s" % e)
        print()
        print("   Токен, возможно, в порядке, а вот папки нет или она недоступна.")
        if args.provider == "yandex":
            print("   Для Яндекса путь пишется как disk:/Имя папки")
        else:
            print("   Для Google нужен ИДЕНТИФИКАТОР папки из адресной строки,")
            print("   а не её имя: drive.google.com/drive/folders/ЭТО_ОН")
        return 4
    print("   Токен принят, папка открылась за %.1f с" % (time.time() - t0))
    print()

    # --- 2. Что внутри ------------------------------------------------------
    print("2. Смотрю содержимое (глубина %d)..." % args.depth)
    media, other, folders = [], 0, 0

    def walk(fid, prefix, depth):
        nonlocal other, folders
        try:
            entries = prov.list_folder(fid)
        except sar_cloud.CloudError as e:
            print("   ! не удалось открыть %s: %s" % (prefix or "папку", e))
            return
        for it in entries:
            rel = (prefix + "/" + it.name) if prefix else it.name
            if it.is_folder:
                folders += 1
                if depth < args.depth:
                    walk(it.id, rel, depth + 1)
                continue
            ext = os.path.splitext(it.name)[1].lower()
            if ext in sar_common.MEDIA_EXTS:
                media.append((rel, it.size, it.id))
            else:
                other += 1

    walk(folder, "", 0)

    total = sum(m[1] for m in media)
    print("   папок:            %d" % folders)
    print("   материала:        %d файлов, %s" % (len(media), human(total)))
    print("   прочих файлов:    %d (платформа их не тронет)" % other)
    print()

    if not media:
        print("   МАТЕРИАЛА НЕ НАЙДЕНО.")
        print("   Платформа считает материалом только видео и снимки:")
        print("   %s" % ", ".join(sorted(sar_common.MEDIA_EXTS)))
        print("   Если файлы там точно есть -- проверьте папку и глубину")
        print("   (--folder, --depth).")
        return 5

    media.sort(key=lambda m: m[1])
    print("   Самый маленький:  %s (%s)" % (media[0][0], human(media[0][1])))
    print("   Самый большой:    %s (%s)" % (media[-1][0], human(media[-1][1])))
    print()

    # --- 3. Сойдётся ли с ограничениями ------------------------------------
    print("3. Сверяю с настройками платформы...")
    try:
        cfg, _ = sar_common.load_server_config(os.path.dirname(os.path.abspath(__file__)))
        _, data_dir, db_path, _ = sar_common.resolve_paths(
            cfg["watch_dir"], cfg.get("data_dir"))
        conn = sar_common.get_db_connection(db_path)
        settings = sar_common.get_settings(conn)
        conn.close()
    except Exception as e:
        print("   не удалось прочитать настройки (%s) -- пропускаю" % e)
        return 0

    cap = float(settings["staging_cap_gb"]) * 1e9
    biggest = media[-1][1]
    print("   потолок временной папки: %s" % human(cap))
    print("   самый большой файл:      %s" % human(biggest))
    if biggest > cap:
        print()
        print("   ВНИМАНИЕ: самый большой файл НЕ ПОМЕСТИТСЯ во временную папку.")
        print("   Обработка по нему не начнётся. Поднимите потолок в /admin")
        print("   минимум до %.0f ГБ." % (biggest / 1e9 + 1))
    else:
        print("   помещается")

    free = None
    try:
        import shutil
        free = shutil.disk_usage(data_dir).free
    except Exception:
        pass
    if free is not None:
        print("   свободно на диске с базой: %s" % human(free))
        if free < cap:
            print()
            print("   ВНИМАНИЕ: свободного места МЕНЬШЕ, чем потолок папки.")
            print("   Снизьте потолок либо освободите диск -- иначе платформа")
            print("   упрётся в реальный конец диска раньше своего лимита.")
    print()

    # --- 4. Доходят ли байты ------------------------------------------------
    if args.download_smallest:
        rel, size, fid = media[0]
        print("4. Качаю самый маленький файл (%s, %s)..." % (rel, human(size)))
        tmp = os.path.join(tempfile.gettempdir(), "sar_cloud_check.bin")
        t0 = time.time()
        try:
            got = prov.download(fid, tmp, expected_size=size)
        except sar_cloud.CloudError as e:
            print("   ОТКАЗ: %s" % e)
            return 6
        finally:
            pass
        secs = max(time.time() - t0, 0.001)
        print("   получено %s за %.1f с (%.1f Мбит/с)"
              % (human(got), secs, got * 8 / secs / 1e6))
        try:
            os.remove(tmp)
        except OSError:
            pass
        print("   временный файл удалён")
        print()
        if total and got:
            est = total / (got / secs)
            print("   При такой скорости вся папка (%s) скачается примерно"
                  % human(total))
            print("   за %.1f ч -- однократно, по мере обработки." % (est / 3600))
        print()

    print("ГОТОВО. Подключение рабочее.")
    print("Теперь тот же токен и ту же папку можно вводить в /admin.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
