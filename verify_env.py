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
    print("      Project Setup Verification Tool")
    print("-" * 50)

    if env_path.exists():
        print(f"[OK] Found .env file at: {env_path}")
        load_dotenv(dotenv_path=env_path)
    else:
        print(f"[!] Warning: .env file not found at: {env_path}")
        print("    Please copy .env.example to .env and fill in your API keys.")
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

    print("\nCritical API Keys:")
    for var in critical_vars:
        val = os.getenv(var)
        if val:
            masked = val[:5] + "*" * (len(val) - 10) + val[-5:] if len(val) > 10 else "***"
            print(f"  [OK] {var:20}: {masked}")
        else:
            print(f"  [MISSING] {var:20}")
            all_fine = False

    print("\nOptional Configuration:")
    for var in optional_vars:
        val = os.getenv(var)
        status = f"Set ({val})" if val else "Not set (using defaults)"
        print(f"  {var:20}: {status}")

    print("\n" + "-" * 50)
    if all_fine:
        print("[SUCCESS] Your environment appears to be correctly configured!")
        print("   You can now run the app:")
        print("   Web API: python -m uvicorn api.main:app --reload")
        print("   Docs: http://127.0.0.1:8000/docs")
    else:
        print("[ACTION REQUIRED] Some critical API keys are missing.")
        print("   Check your .env file and ensure all keys are correctly pasted.")
        print("   Refer to README.md for instructions on how to obtain these keys.")
    print("-" * 50)


if __name__ == "__main__":
    verify_env()
