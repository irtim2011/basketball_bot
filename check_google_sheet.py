"""Check the service-account file and access to the configured bot tab."""
import google_sheet


def main():
    try:
        result = google_sheet.check_blocking()
    except FileNotFoundError as exc:
        raise SystemExit(
            f"{exc}\nЗагрузите JSON-ключ на сервер и выполните: "
            "training-bot google-connect /путь/к/ключу.json"
        ) from None
    except Exception as exc:
        raise SystemExit(
            f"Google Таблица недоступна: {type(exc).__name__}. "
            "Проверьте, что таблица открыта сервисному аккаунту как редактору."
        ) from None
    print(f"Google Таблица подключена: {result['title']}")
    print(result["url"])


if __name__ == "__main__":
    main()
