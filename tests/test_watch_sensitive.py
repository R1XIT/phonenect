"""watch_clipboard: секретное не публикуется, в том числе при гонке между проверкой и чтением. Буфер подменён."""
from phonenect import hub as hub_mod


class FakeLoop:
    def __init__(self):
        self.calls = []

    def call_soon_threadsafe(self, fn, *args):
        self.calls.append(args)


class FakeCb:
    def __init__(self):
        self.seq = 1
        self.secret = False
        self.check_raises = False
        self.content = ("text", "hello")
        self.on_sensitive = None  # колбэк, который выполняется внутри проверки (имитация гонки)

    def sequence_number(self):
        return self.seq

    def sensitive(self):
        if self.on_sensitive:
            self.on_sensitive()
        if self.check_raises:
            raise OSError("busy")
        return self.secret

    def read(self):
        return self.content


def make(monkeypatch, tmp_path):
    fake = FakeCb()
    monkeypatch.setattr(hub_mod, "cb", fake)
    h = hub_mod.Hub(tmp_path, "pc", "id")
    h.loop = FakeLoop()
    return h, fake


def test_normal_copy_is_published(monkeypatch, tmp_path):
    h, fake = make(monkeypatch, tmp_path)
    fake.seq = 2
    h._poll_clipboard()
    assert [c[0] for c in h.loop.calls] == ["text"]


def test_sensitive_copy_is_not_published(monkeypatch, tmp_path):
    h, fake = make(monkeypatch, tmp_path)
    fake.seq, fake.secret = 2, True
    h._poll_clipboard()
    assert h.loop.calls == []


def test_sensitivity_check_error_skips_copy(monkeypatch, tmp_path):
    h, fake = make(monkeypatch, tmp_path)
    fake.seq, fake.check_raises = 2, True
    h._poll_clipboard()
    assert h.loop.calls == []


def test_race_between_check_and_read_drops_content(monkeypatch, tmp_path):
    h, fake = make(monkeypatch, tmp_path)
    fake.seq = 2
    fake.on_sensitive = lambda: setattr(fake, "seq", 3)  # менеджер паролей копирует после проверки
    h._poll_clipboard()
    assert h.loop.calls == []
    # Следующий опрос видит новое копирование и проверяет его: секрет по-прежнему не публикуется.
    fake.on_sensitive = None
    fake.secret = True
    h._poll_clipboard()
    assert h.loop.calls == []


def test_race_then_normal_copy_is_published_next_poll(monkeypatch, tmp_path):
    h, fake = make(monkeypatch, tmp_path)
    fake.seq = 2
    fake.on_sensitive = lambda: setattr(fake, "seq", 3)
    h._poll_clipboard()
    fake.on_sensitive = None
    h._poll_clipboard()
    assert [c[0] for c in h.loop.calls] == ["text"]
