"""`itu present` launcher and the read-only network checks (no robot, no network changes)."""

from __future__ import annotations

import argparse

import pytest

from is_this_us import netcheck

MAC_IFCONFIG = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
	inet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
	ether 11:22:33:44:55:66
	inet 192.168.1.23 netmask 0xffffff00 broadcast 192.168.1.255
	status: active
en6: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
	ether aa:bb:cc:dd:ee:ff
	inet 192.168.123.100 netmask 0xffffff00 broadcast 192.168.123.255
	media: autoselect (1000baseT <full-duplex>)
	status: active
en7: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
	ether aa:bb:cc:dd:ee:00
	media: autoselect (none)
	status: inactive
"""


def test_parse_macos_ifconfig():
    found = netcheck.parse_ifconfig(MAC_IFCONFIG)
    assert found["en6"].active and found["en6"].in_subnet() == ["192.168.123.100"]
    assert not found["en7"].active and found["en0"].in_subnet() == []


def test_link_ok_and_every_failure_is_explained():
    found = netcheck.parse_ifconfig(MAC_IFCONFIG)
    assert netcheck.check_link("en6", found=found).ok
    gone = netcheck.check_link("en5", found=found)  # adapter unplugged / renamed
    assert not gone.ok and "does not exist" in gone.message and "en6" in gone.message and gone.candidates == ["en6"]
    nolink = netcheck.check_link("en7", found=found)
    assert not nolink.ok and "no link" in nolink.message
    wrong = netcheck.check_link("en0", found=found)
    assert not wrong.ok and "192.168.1.23" in wrong.message
    none = netcheck.check_link("en6", found={k: v for k, v in found.items() if k != "en6"})
    assert not none.ok and "Manually" in none.message  # tells the user what to set; changes nothing itself


def _args(**kw):
    base = dict(pose_camera="robot", interface="en6", robot_ip="192.168.123.161", camera="synthetic:sitting", fallback="", motion=False,
                walk=False, i_have_physical_estop=False, telegram=False, no_robot=False, no_voice=True,
                rounds_only=False, force=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_launcher_refuses_motion_without_the_estop_flag():
    from is_this_us.present_cli import cmd_present

    with pytest.raises(SystemExit, match="i-have-physical-estop"):
        cmd_present(_args(motion=True))
    with pytest.raises(SystemExit, match="needs --motion"):
        cmd_present(_args(walk=True, i_have_physical_estop=True))


def test_checks_block_motion_without_robot_and_walking_without_calibration(monkeypatch, capsys):
    from is_this_us.config import Settings
    from is_this_us import present_cli

    monkeypatch.setattr(netcheck, "interfaces", lambda: {})
    monkeypatch.setattr(present_cli, "_bridge_status", lambda h, p: (False, "no answer"))
    s = Settings(_env_file=None)
    assert present_cli.run_checks(_args(motion=True, i_have_physical_estop=True), s) is False
    out = capsys.readouterr().out
    assert "does not exist" in out and "start_bridge.sh" in out
    # projector-only presentation may still start
    assert present_cli.run_checks(_args(), s) is True
    found = netcheck.parse_ifconfig(MAC_IFCONFIG)
    monkeypatch.setattr(netcheck, "interfaces", lambda: found)
    monkeypatch.setattr(netcheck, "ping", lambda host, timeout_s=1.0: True)
    monkeypatch.setattr(present_cli, "_bridge_status", lambda h, p: (True, {"sport_timeout_s": 10.0}))
    monkeypatch.setattr(present_cli, "_camera_ok", lambda spec: (True, spec))
    assert present_cli.run_checks(_args(motion=True, i_have_physical_estop=True), s) is True
    assert present_cli.run_checks(_args(motion=True, walk=True, i_have_physical_estop=True), s) is False  # uncalibrated
    assert "not calibrated" in capsys.readouterr().out


def test_launcher_runs_without_telegram_by_default(monkeypatch):
    from is_this_us import present_cli
    from is_this_us.config import get_settings
    from is_this_us.performance import cli as show_cli

    seen = {}
    monkeypatch.setattr(present_cli, "run_checks", lambda a, s: True)
    monkeypatch.setattr(show_cli, "cmd_show", lambda a: seen.update(args=a, settings=get_settings()) or 0)
    monkeypatch.setenv("SHOW_TELEGRAM_CHAT_ID", "-100999")
    import os

    saved = dict(os.environ)
    try:
        assert present_cli.cmd_present(_args(no_robot=True)) == 0
        st = seen["settings"]
        assert st.show_telegram_chat_id in (None, "", 0) and not seen["args"].telegram
        assert st.show_programme == "presentation" and st.show_walk_enabled is False and not seen["args"].motion
    finally:
        os.environ.clear()
        os.environ.update(saved)  # the launcher sets this run's settings in-process; don't leak into other tests
        get_settings.cache_clear()
