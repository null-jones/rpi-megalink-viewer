"""The display's configuration file."""

from __future__ import annotations

import json

import pytest

from megalink_viewer.config import (
    CONFIG_ENV,
    Config,
    ConfigError,
    default_path,
    load,
    save,
)


class TestDefaults:
    def test_a_fresh_config_is_not_configured(self):
        config = Config()
        assert not config.configured
        assert config.describe() == "not configured"

    def test_a_club_and_a_lane_are_enough(self):
        assert Config(host="stord-pk", lane="9").configured

    def test_a_club_alone_is_not(self):
        assert not Config(host="stord-pk").configured

    def test_name_falls_back_to_the_hostname(self):
        assert Config().name
        assert Config(host="x").replace().name == Config().name

    def test_an_explicit_name_wins(self):
        config = Config()
        config.beacon.name = "firing-point-9"
        assert config.name == "firing-point-9"

    def test_describe(self):
        assert Config(host="stord-pk", range="1-10", lane="9").describe() == (
            "stord-pk · 1-10 · lane 9"
        )


class TestCoercion:
    def test_a_numeric_lane_becomes_text(self):
        # Some ranges label firing points with letters, so lanes are text.
        assert Config.from_dict({"lane": 9}).lane == "9"

    def test_whitespace_is_trimmed(self):
        assert Config.from_dict({"host": "  stord-pk "}).host == "stord-pk"

    def test_a_string_interval_becomes_a_number(self):
        assert Config.from_dict({"display": {"interval": "1.5"}}).display.interval == 1.5

    def test_zero_size_means_unset(self):
        display = Config.from_dict({"display": {"width": 0, "height": ""}}).display
        assert display.width is None and display.height is None

    def test_unknown_keys_are_ignored(self):
        """An old display reading a newer file must still start."""
        config = Config.from_dict(
            {"host": "x", "lane": "1", "future": True, "display": {"mode": "gui", "novel": 1}}
        )
        assert config.host == "x"

    def test_a_non_object_is_rejected(self):
        with pytest.raises(ConfigError):
            Config.from_dict(["not", "an", "object"])


class TestValidation:
    def test_a_bad_mode_is_rejected(self):
        config = Config.from_dict({"display": {"mode": "hologram"}})
        with pytest.raises(ConfigError, match=r"display\.mode"):
            config.validate()

    @pytest.mark.parametrize("interval", [0.0, 0.01, 61, 10_000])
    def test_an_impossible_interval_is_rejected(self, interval):
        config = Config.from_dict({"display": {"interval": interval}})
        with pytest.raises(ConfigError, match="interval"):
            config.validate()

    def test_a_bad_port_is_rejected(self):
        config = Config.from_dict({"web": {"port": 70000}})
        with pytest.raises(ConfigError, match=r"web\.port"):
            config.validate()

    def test_port_zero_is_allowed(self):
        # 0 means "ask the OS for a free port".
        Config.from_dict({"web": {"port": 0}}).validate()

    def test_a_bad_beacon_interval_is_rejected(self):
        config = Config.from_dict({"beacon": {"interval": 0.1}})
        with pytest.raises(ConfigError, match=r"beacon\.interval"):
            config.validate()

    def test_an_absurd_host_is_rejected(self):
        with pytest.raises(ConfigError, match="implausibly long"):
            Config(host="x" * 500).validate()


class TestMerging:
    def test_a_partial_update_leaves_the_rest_alone(self):
        """The web page sends only what changed."""
        config = Config(host="stord-pk", range="1-10", lane="9")
        config.beacon.name = "point-9"
        updated = config.merged({"lane": "3"})
        assert updated.lane == "3"
        assert updated.host == "stord-pk"
        assert updated.beacon.name == "point-9"

    def test_a_section_update_is_merged_not_replaced(self):
        config = Config()
        config.display.interval = 2.0
        updated = config.merged({"display": {"fullscreen": False}})
        assert updated.display.fullscreen is False
        assert updated.display.interval == 2.0

    def test_unknown_top_level_keys_are_dropped(self):
        assert not hasattr(Config().merged({"nonsense": 1}), "nonsense")

    def test_an_invalid_update_is_refused(self):
        with pytest.raises(ConfigError):
            Config().merged({"display": {"mode": "smoke signals"}})

    def test_a_non_object_update_is_refused(self):
        with pytest.raises(ConfigError):
            Config().merged("lane=3")


class TestSecrets:
    def test_the_token_is_never_handed_out(self):
        config = Config()
        config.web.token = "hunter2"
        assert config.public_dict()["web"]["token"] is True
        assert config.to_dict()["web"]["token"] == "hunter2"

    def test_no_token_reads_as_false(self):
        assert Config().public_dict()["web"]["token"] is False


class TestPersistence:
    def test_round_trip(self, config_path):
        config = Config(host="stord-pk", range="1-10", lane="9")
        config.web.token = "secret"
        save(config, config_path)
        loaded = load(config_path)
        assert loaded.describe() == config.describe()
        assert loaded.web.token == "secret"

    def test_saving_creates_the_directory(self, tmp_path):
        target = tmp_path / "nested" / "deeper" / "display.json"
        save(Config(host="x", lane="1"), target)
        assert target.exists()

    def test_the_file_is_readable_json(self, config_path):
        save(Config(host="x", lane="1"), config_path)
        assert json.loads(config_path.read_text())["host"] == "x"

    def test_a_missing_file_gives_defaults(self, tmp_path):
        assert not load(tmp_path / "absent.json").configured

    def test_an_empty_file_reads_as_no_configuration(self, config_path):
        """An interrupted write leaves an empty file; that must not be fatal.

        There is nothing in an empty file to lose by starting from defaults, and
        refusing to start over one leaves a display dead for a reason nobody at
        the range can act on.
        """
        config_path.write_text("", encoding="utf-8")
        assert not load(config_path).configured

    def test_a_whitespace_only_file_reads_as_no_configuration(self, config_path):
        config_path.write_text("  \n\t\n", encoding="utf-8")
        assert not load(config_path).configured

    def test_malformed_json_is_still_refused(self, config_path):
        """A typo is different: it holds intent, and must not be silently dropped."""
        config_path.write_text('{"host": "stord-pk",,}', encoding="utf-8")
        with pytest.raises(ConfigError, match="not valid JSON"):
            load(config_path)

    def test_malformed_json_is_reported_clearly(self, config_path):
        config_path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ConfigError, match="not valid JSON"):
            load(config_path)

    def test_an_invalid_file_is_refused(self, config_path):
        config_path.write_text('{"display": {"mode": "nope"}}', encoding="utf-8")
        with pytest.raises(ConfigError):
            load(config_path)

    def test_saving_leaves_no_temporary_files_behind(self, config_path):
        save(Config(host="x", lane="1"), config_path)
        assert [p.name for p in config_path.parent.iterdir()] == [config_path.name]

    def test_a_save_never_leaves_a_half_written_file(self, config_path, monkeypatch):
        """The display reads this while the web page writes it."""
        save(Config(host="first", lane="1"), config_path)
        import megalink_viewer.config as module

        def explode(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(module.os, "replace", explode)
        with pytest.raises(OSError):
            save(Config(host="second", lane="2"), config_path)
        # The previous document survived intact.
        assert load(config_path).host == "first"
        assert [p.name for p in config_path.parent.iterdir()] == [config_path.name]


class TestPathResolution:
    def test_the_environment_wins(self, tmp_path, monkeypatch):
        target = tmp_path / "elsewhere.json"
        monkeypatch.setenv(CONFIG_ENV, str(target))
        assert default_path() == target

    def test_the_user_directory_is_the_fallback(self, tmp_path, monkeypatch):
        monkeypatch.delenv(CONFIG_ENV, raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setattr("megalink_viewer.config.SYSTEM_PATH", tmp_path / "nonexistent")
        assert default_path() == tmp_path / "megalink" / "display.json"


class TestDurableWrites:
    """A display is unplugged, not shut down, so every write must survive that.

    This project kept finding zero-length files on its Pis -- the config, the
    systemd unit, a venv binary -- which is what a rename that reaches the disk
    ahead of its data leaves behind after a power cut.
    """

    def test_the_data_is_flushed_before_the_rename(self, tmp_path, monkeypatch):
        from megalink_viewer import config as module

        order = []
        real_fsync, real_replace = module.os.fsync, module.os.replace
        monkeypatch.setattr(
            module.os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1]
        )
        monkeypatch.setattr(
            module.os, "replace", lambda a, b: (order.append("replace"), real_replace(a, b))[1]
        )
        module.write_durably(tmp_path / "display.json", b"{}")
        assert order.index("fsync") < order.index("replace"), order

    def test_the_directory_is_flushed_after_the_rename(self, tmp_path, monkeypatch):
        # The rename is recorded in the directory, so it needs flushing too.
        from megalink_viewer import config as module

        order = []
        real_fsync, real_replace = module.os.fsync, module.os.replace
        monkeypatch.setattr(
            module.os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1]
        )
        monkeypatch.setattr(
            module.os, "replace", lambda a, b: (order.append("replace"), real_replace(a, b))[1]
        )
        module.write_durably(tmp_path / "display.json", b"{}")
        assert order == ["fsync", "replace", "fsync"], order

    def test_the_content_arrives(self, tmp_path):
        from megalink_viewer.config import write_durably

        target = tmp_path / "x.json"
        write_durably(target, b'{"a": 1}')
        assert target.read_bytes() == b'{"a": 1}'

    def test_no_temporary_is_left_behind(self, tmp_path):
        from megalink_viewer.config import write_durably

        write_durably(tmp_path / "x.json", b"{}")
        assert [p.name for p in tmp_path.iterdir()] == ["x.json"]

    def test_a_failed_write_leaves_the_old_file_alone(self, tmp_path, monkeypatch):
        from megalink_viewer import config as module

        target = tmp_path / "display.json"
        target.write_bytes(b'{"old": true}')

        def fail(_fd):
            raise OSError("disk full")

        monkeypatch.setattr(module.os, "fsync", fail)
        with pytest.raises(OSError):
            module.write_durably(target, b'{"new": true}')
        assert target.read_bytes() == b'{"old": true}'
        assert [p.name for p in tmp_path.iterdir()] == ["display.json"]

    def test_a_directory_that_cannot_be_flushed_is_not_fatal(self, tmp_path, monkeypatch):
        from megalink_viewer import config as module

        real_open = module.os.open

        def no_dirs(path, flags, *rest):
            if str(path) == str(tmp_path):
                raise OSError("not on this platform")
            return real_open(path, flags, *rest)

        monkeypatch.setattr(module.os, "open", no_dirs)
        module.write_durably(tmp_path / "x.json", b"{}")
        assert (tmp_path / "x.json").read_bytes() == b"{}"

    def test_saving_the_configuration_goes_through_it(self, tmp_path, monkeypatch):
        from megalink_viewer import config as module

        calls = []
        monkeypatch.setattr(module, "write_durably", lambda t, d, prefix="": calls.append(t))
        module.save(module.Config(host="a", range="b", lane="1"), tmp_path / "d.json")
        assert calls == [tmp_path / "d.json"]
