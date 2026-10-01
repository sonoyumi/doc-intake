"""Точка входа: `python -m doc_intake` или команда `doc-intake`."""


def greet(name: str) -> str:
    return f"Привет, {name}!"


def main() -> None:
    print(greet("мир"))


if __name__ == "__main__":
    main()
