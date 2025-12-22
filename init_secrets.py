import os, base64

def write_from_b64(env_name: str, out_path: str):
    b64 = os.getenv(env_name, "")
    if not b64:
        raise RuntimeError(f"Missing env var: {env_name}")
    data = base64.b64decode(b64.encode("utf-8"))
    with open(out_path, "wb") as f:
        f.write(data)

if __name__ == "__main__":
    write_from_b64("SERVICE_ACCOUNT_JSON_B64", "service_account.json")
    write_from_b64("TOKEN_DRIVE_JSON_B64", "token_drive.json")
    print("Secrets written.")
