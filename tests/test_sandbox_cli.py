from argparse import Namespace

from sandbox_env import cli


class FakeBackend:
    name = "ssh"

    def is_ready(self):
        return True


class FakeCatalog:
    def categories(self):
        return ["ctf-pwn"]

    def allowed_tools(self, category):
        assert category == "ctf-pwn"
        return ["gdb"]


class FakeTools:
    catalog = FakeCatalog()


class FakeManager:
    backend = FakeBackend()
    tools = FakeTools()

    def __init__(self):
        self.closed = False

    def session_key(self):
        return "session"

    async def ensure(self, session_key):
        assert session_key == "session"
        return "ctf-session"

    async def install_tools(self, tool_ids, *, session_key, force):
        assert tool_ids == ["gdb"]
        assert session_key == "session"
        assert force is False
        return {
            "installed": ["gdb"],
            "failed": [],
            "skipped_manual": [],
            "incompatible": [],
        }

    async def close(self):
        self.closed = True


def test_sandbox_probe_awaits_backend_and_closes(monkeypatch, capsys):
    manager = FakeManager()
    monkeypatch.setattr(cli, "_manager", lambda: manager)

    cli.cmd_sandbox_probe(Namespace())

    assert manager.closed is True
    assert "session_container=ctf-session" in capsys.readouterr().out


def test_sandbox_deps_awaits_install_and_closes(monkeypatch, capsys):
    manager = FakeManager()
    monkeypatch.setattr(cli, "_manager", lambda: manager)

    cli.cmd_sandbox_deps(Namespace(tools=["ctf-pwn"], force=False))

    assert manager.closed is True
    assert "installed=['gdb']" in capsys.readouterr().out
