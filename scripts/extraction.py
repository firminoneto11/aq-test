from typing import TextIO, cast


def _extract_external_secrets():
    # NOTE: To be implemented later once external secret storage is chosen
    return cast(dict[str, str], {})


def main(file_stream: TextIO):
    secrets = _extract_external_secrets()
    for key, val in secrets.items():
        escaped = val.replace("\\", "\\\\").replace('"', '\\"')
        file_stream.write(f'{key}="{escaped}"\n')


if __name__ == "__main__":
    with open(".env", mode="w") as file_stream:
        main(file_stream=file_stream)
