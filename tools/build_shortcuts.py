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
WIFI_QUESTION = "Название домашней сети Wi-Fi (как на странице Phonenect). Вне её команда ничего не делает."


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


def uid() -> str:
    return str(uuid.uuid4()).upper()


def output(action_uuid: str, name: str) -> dict:
    return {"Type": "ActionOutput", "OutputUUID": action_uuid, "OutputName": name}


def if_start(group: str, value: dict, condition: int, string: str | None = None) -> dict:
    params = {
        "GroupingIdentifier": group,
        "WFControlFlowMode": 0,
        "WFCondition": condition,
        "WFInput": {"Type": "Variable", "Variable": attachment(value)},
    }
    if string is not None:
        params["WFConditionalActionString"] = string
    return action("is.workflow.actions.conditional", **params)


def if_end(group: str) -> dict:
    return action("is.workflow.actions.conditional", GroupingIdentifier=group, WFControlFlowMode=2)


def question(index: int, key: str, prompt: str) -> dict:
    return {"ActionIndex": index, "Category": "Parameter", "ParameterKey": key, "Text": prompt, "DefaultValue": ""}


COND_IS = 4
COND_HAS_VALUE = 100


def workflow(actions: list, color: int, glyph: int, questions: list | None = None, **extra) -> dict:
    return {
        "WFWorkflowClientVersion": "2607.0.2",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": color, "WFWorkflowIconGlyphNumber": glyph},
        # По умолчанию первое действие — «Получить содержимое URL».
        "WFWorkflowImportQuestions": questions or [question(0, "WFURL", URL_QUESTION)],
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


def home_wifi_guard(group: str) -> list:
    """Начало команды для автоматизаций: дальше идём только в домашнем Wi-Fi.
    Иначе вне дома каждое открытие приложения заканчивалось бы ошибкой соединения."""
    wifi = uid()
    return [
        action("is.workflow.actions.getwifi", UUID=wifi, WFNetworkDetailsNetwork="Wi-Fi", WFWiFiDetail="Network Name"),
        if_start(group, output(wifi, "Network Details"), COND_IS, ""),
    ]


def auto_questions(url_index: int) -> list:
    # Индекс 1 — «Если» из home_wifi_guard.
    return [
        question(1, "WFConditionalActionString", WIFI_QUESTION),
        question(url_index, "WFURL", URL_QUESTION),
    ]


def auto_from_pc() -> dict:
    """Для автоматизации «При открытии приложения»: забрать буфер ПК, если там что-то новое."""
    home, has_new, fetch = uid(), uid(), uid()
    return workflow(
        [
            *home_wifi_guard(home),
            action(
                "is.workflow.actions.downloadurl",
                UUID=fetch,
                WFURL="",
                ShowHeaders=True,
                WFHTTPHeaders=headers({"X-Device": "iPhone", "X-Only-New": "1"}),
            ),
            # Нового нет — сервер отвечает пустым 204, и буфер iPhone не трогаем.
            if_start(has_new, output(fetch, "Contents of URL"), COND_HAS_VALUE),
            action("is.workflow.actions.setclipboard", WFInput=attachment(output(fetch, "Contents of URL")), WFLocalOnly=False),
            notify("Скопировано с ПК"),
            if_end(has_new),
            if_end(home),
        ],
        color=4292093695,
        glyph=59511,
        questions=auto_questions(url_index=2),
        WFWorkflowTypes=[],
        WFWorkflowInputContentItemClasses=[],
    )


def auto_to_pc() -> dict:
    """Для автоматизации «При закрытии приложения»: тихо отправить буфер iPhone.
    Сервер сам отбрасывает то, что уже видел, поэтому свежий буфер ПК не затрётся."""
    home, clip = uid(), uid()
    return workflow(
        [
            *home_wifi_guard(home),
            action("is.workflow.actions.getclipboard", UUID=clip),
            action(
                "is.workflow.actions.downloadurl",
                UUID=uid(),
                WFURL="",
                WFHTTPMethod="POST",
                WFHTTPBodyType="File",
                WFRequestVariable=attachment(output(clip, "Clipboard")),
                ShowHeaders=True,
                WFHTTPHeaders=headers({"X-Device": "iPhone", "X-Auto": "1"}),
            ),
            if_end(home),
        ],
        color=463140863,
        glyph=59511,
        questions=auto_questions(url_index=3),
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


SHORTCUTS = {
    "to-pc": ("На ПК", to_pc),
    "from-pc": ("С ПК", from_pc),
    "auto-to-pc": ("Авто — На ПК", auto_to_pc),
    "auto-from-pc": ("Авто — С ПК", auto_from_pc),
}


def main() -> None:
    """Аргументы — какие команды собрать (по умолчанию все): build_shortcuts.py auto-to-pc"""
    OUT.mkdir(parents=True, exist_ok=True)
    targets = [a for a in sys.argv[1:] if not a.startswith("--")] or list(SHORTCUTS)
    for slug in targets:
        name, build = SHORTCUTS[slug]
        wf = build()
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
