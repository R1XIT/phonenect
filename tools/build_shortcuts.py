"""Собирает команды iPhone «На ПК» и «С ПК» и подписывает их через HubSign (RoutineHub).

iOS принимает только подписанные команды, а подписать без Mac можно лишь через такой сервис.
Команды — общий шаблон без адреса и токена: адрес iPhone спрашивает при добавлении
(вопрос при импорте), поэтому на подпись не уходит ничего личного.

Запуск: .venv\\Scripts\\python tools\\build_shortcuts.py
"""
import plistlib
import subprocess
import sys
import uuid
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "phonenect" / "web" / "shortcuts"
HUBSIGN = "https://hubsign.routinehub.services/sign"
URL_QUESTION = "Адрес Phonenect. Он уже скопирован на странице — вставьте его сюда."


def text(s: str) -> dict:
    return {"Value": {"string": s, "attachmentsByRange": {}}, "WFSerializationType": "WFTextTokenString"}


def attachment(value: dict) -> dict:
    return {"Value": value, "WFSerializationType": "WFTextTokenAttachment"}


def headers(items: dict) -> dict:
    return {
        "Value": {
            "WFDictionaryFieldValueItems": [
                {"WFItemType": 0, "WFKey": text(k), "WFValue": text(v)} for k, v in items.items()
            ]
        },
        "WFSerializationType": "WFDictionaryFieldValue",
    }


def action(identifier: str, **params) -> dict:
    return {"WFWorkflowActionIdentifier": identifier, "WFWorkflowActionParameters": params}


def notify(body: str) -> dict:
    return action("is.workflow.actions.notification", WFNotificationActionBody=text(body), WFNotificationActionSound=False)


def workflow(actions: list, color: int, glyph: int, **extra) -> dict:
    return {
        "WFWorkflowClientVersion": "2607.0.2",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": color, "WFWorkflowIconGlyphNumber": glyph},
        "WFWorkflowImportQuestions": [
            # Первое действие в обеих командах — «Получить содержимое URL».
            {"ActionIndex": 0, "Category": "Parameter", "ParameterKey": "WFURL", "Text": URL_QUESTION, "DefaultValue": ""}
        ],
        "WFWorkflowActions": actions,
        "WFWorkflowOutputContentItemClasses": [],
        "WFQuickActionSurfaces": [],
        **extra,
    }


def to_pc() -> dict:
    return workflow(
        [
            action(
                "is.workflow.actions.downloadurl",
                UUID=str(uuid.uuid4()).upper(),
                WFURL="",
                WFHTTPMethod="POST",
                WFHTTPBodyType="File",
                WFRequestVariable=attachment({"Type": "ExtensionInput"}),
                ShowHeaders=True,
                WFHTTPHeaders=headers({"X-Device": "iPhone"}),
            ),
            notify("Отправлено на ПК"),
        ],
        color=463140863,
        glyph=59511,
        # Работает из меню «Поделиться»; без входных данных (жест, виджет) берёт буфер обмена.
        WFWorkflowTypes=["ActionExtension"],
        WFWorkflowHasShortcutInputVariables=True,
        WFWorkflowInputContentItemClasses=[
            "WFImageContentItem",
            "WFStringContentItem",
            "WFRichTextContentItem",
            "WFURLContentItem",
            "WFGenericFileContentItem",
        ],
        WFWorkflowNoInputBehavior={"Name": "WFWorkflowNoInputBehaviorGetClipboard", "Parameters": {}},
    )


def from_pc() -> dict:
    fetch_id = str(uuid.uuid4()).upper()
    return workflow(
        [
            action("is.workflow.actions.downloadurl", UUID=fetch_id, WFURL=""),
            action(
                "is.workflow.actions.setclipboard",
                WFInput=attachment({"Type": "ActionOutput", "OutputUUID": fetch_id, "OutputName": "Contents of URL"}),
                WFLocalOnly=False,
            ),
            notify("Скопировано с ПК"),
        ],
        color=4292093695,
        glyph=59511,
        WFWorkflowTypes=[],
        WFWorkflowInputContentItemClasses=[],
    )


def sign(name: str, plist_path: Path) -> bytes:
    # Форма multipart, как в веб-интерфейсе HubSign. Системный curl (Schannel), потому что
    # некоторые VPN/фильтры рвут TLS-соединения Python.
    data = subprocess.run(
        ["curl.exe", "-sS", "--fail", "-m", "120", "-X", "POST", HUBSIGN,
         "-F", f"shortcutName={name}",
         "-F", f"shortcut=@{plist_path};type=application/xml"],
        capture_output=True, check=True,
    ).stdout
    if not data.startswith(b"AEA1"):
        raise RuntimeError(f"HubSign вернул не подписанную команду: {data[:200]!r}")
    return data


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for slug, name, wf in [("to-pc", "На ПК", to_pc()), ("from-pc", "С ПК", from_pc())]:
        plist_path = OUT / f"{slug}.plist"
        plist_path.write_bytes(plistlib.dumps(wf, fmt=plistlib.FMT_XML))
        if "--no-sign" in sys.argv:
            continue
        # Кириллица в аргументах curl.exe искажается; имя команды iOS всё равно берёт из имени файла.
        signed = sign(slug, plist_path)
        (OUT / f"{slug}.shortcut").write_bytes(signed)
        print(f"{name}: {len(signed)} байт -> {OUT / f'{slug}.shortcut'}")


if __name__ == "__main__":
    main()
