import os
from pathlib import Path

from dotenv import load_dotenv


def verify_env():
    """
    Verifies that critical environment variables are set and the .env file is accessible.
    """
    script_dir = Path(__file__).parent
    env_path = script_dir / ".env"

    print("-" * 50)
    print("      Утилита проверки настройки проекта")
    print("-" * 50)

    if env_path.exists():
        print(f"[ОК] Найден файл .env по пути: {env_path}")
        load_dotenv(dotenv_path=env_path)
    else:
        print(f"[!] Предупреждение: файл .env не найден по пути: {env_path}")
        print("    Пожалуйста, скопируйте .env.example в .env и заполните ключи API.")
        print("-" * 50)

    critical_vars = [
        "SERPER_API_KEY",
        "GIGACHAT_AUTH_DATA",
        "PERPLEXITY_API_KEY",
    ]

    optional_vars = [
        "PERPLEXITY_BASE_URL",
        "PERPLEXITY_MODEL",
        "CORS_ORIGINS",
        "DEBUG",
    ]

    all_fine = True

    print("\nКритические ключи API:")
    for var in critical_vars:
        val = os.getenv(var)
        if val:
            masked = val[:5] + "*" * (len(val) - 10) + val[-5:] if len(val) > 10 else "***"
            print(f"  [ОК] {var:20}: {masked}")
        else:
            print(f"  [ОТСУТСТВУЕТ] {var:20}")
            all_fine = False

    print("\nДополнительные настройки:")
    for var in optional_vars:
        val = os.getenv(var)
        status = f"Задано ({val})" if val else "Не задано (используются значения по умолчанию)"
        print(f"  {var:20}: {status}")

    print("\n" + "-" * 50)
    if all_fine:
        print("[УСПЕХ] Ваше окружение настроено правильно!")
        print("   Теперь вы можете запустить приложение:")
        print("   Web API: python -m uvicorn api.main:app --reload")
        print("   Docs: http://127.0.0.1:8000/docs")
    else:
        print("[ТРЕБУЕТСЯ ДЕЙСТВИЕ] Отсутствуют некоторые критические ключи API.")
        print("   Проверьте файл .env и убедитесь, что все ключи вставлены правильно.")
        print("   Обратитесь к README.md за инструкциями по получению этих ключей.")
    print("-" * 50)


if __name__ == "__main__":
    verify_env()
